"""Unit tests for LMU Moodle authentication, Bewerter filtering, PDF naming, partner OCR matching, and upload packaging."""

from __future__ import annotations

import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from moodle_client import (
    ALLOWED_AUTH_HOSTS,
    LMUMoodleClient,
    RawSubmissionRow,
    SecurityError,
    assert_https_and_host,
    extract_trainers_from_participants_html,
    names_match,
)
from pdf_processor import (
    CourseStudent,
    MarkedSubmissionItem,
    StudentSubmissionRecord,
    build_moodle_feedback_zip,
    clear_excluded_partner_names,
    detect_partner_for_record,
    expand_marked_items_with_partners,
    extract_score_from_filename,
    extract_text_and_ocr_from_pdf,
    find_matriculation_anchors,
    format_student_pdf_name,
    is_excluded_partner_name,
    is_totally_typed_page,
    load_manifest,
    load_manifest_with_roster,
    match_marked_pdfs,
    match_partner_in_course_roster,
    merge_pdf_bytes_list,
    read_grades_csv,
    register_excluded_partner_names,
    resolve_partner_by_query,
    save_manifest,
    split_fullname,
    sync_grades_csv,
)


def make_blank_pdf_bytes(
    num_pages: int = 1, text: str = "", totally_typed: bool = False
) -> bytes:
    writer = PdfWriter()
    for idx in range(num_pages):
        page = writer.add_blank_page(width=595, height=842)
        if idx == 0 and text:
            if not totally_typed:
                writer.add_metadata({"/Subject": text})
            font_dict = DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }
            )
            resources = DictionaryObject(
                {
                    NameObject("/Font"): DictionaryObject(
                        {NameObject("/F1"): font_dict}
                    )
                }
            )
            page[NameObject("/Resources")] = resources
            safe_txt = text.replace("(", "\\(").replace(")", "\\)")
            stroke_cmd = ""
            if not totally_typed:
                # Simulate handwritten vector strokes (30 segments) so the page is recognized as handwritten
                stroke_cmd = " 10 10 m " + " ".join(f"{i} {i} l" for i in range(11, 42)) + " S"
            stream = DecodedStreamObject()
            stream.set_data(
                f"BT /F1 12 Tf 72 720 Td ({safe_txt}) Tj ET{stroke_cmd}".encode(
                    "latin-1", "replace"
                )
            )
            page[NameObject("/Contents")] = writer._add_object(stream)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


class TestSecurityAndAuth(unittest.TestCase):
    def test_assert_https_and_host_enforces_allowlist(self) -> None:
        assert_https_and_host("https://moodle.lmu.de/login/index.php", ALLOWED_AUTH_HOSTS)
        assert_https_and_host("https://lmuidp.lrz.de/idp/profile/SAML2/Redirect/SSO", ALLOWED_AUTH_HOSTS)

        with self.assertRaises(SecurityError):
            assert_https_and_host("http://lmuidp.lrz.de/idp", ALLOWED_AUTH_HOSTS)

        with self.assertRaises(SecurityError):
            assert_https_and_host("https://phishing.example.com/idp", ALLOWED_AUTH_HOSTS)

    def test_shibboleth_login_handshake_mock(self) -> None:
        session = MagicMock()

        # 1. e1s1 local storage check form
        r1 = MagicMock()
        r1.url = "https://lmuidp.lrz.de/idp/profile/SAML2/Redirect/SSO?execution=e1s1"
        r1.text = """
        <form name="form1" action="/idp/profile/SAML2/Redirect/SSO?execution=e1s1" method="post">
            <input type="hidden" name="csrf_token" value="token_step1" />
            <input name="shib_idp_ls_supported" type="hidden" />
            <input name="_eventId_proceed" type="hidden" />
        </form>
        """

        # 2. e1s2 username/password form
        r2 = MagicMock()
        r2.url = "https://lmuidp.lrz.de/idp/profile/SAML2/Redirect/SSO?execution=e1s2"
        r2.text = """
        <form action="/idp/profile/SAML2/Redirect/SSO?execution=e1s2" method="post">
            <input type="hidden" name="csrf_token" value="token_step2" />
            <input type="text" name="j_username" />
            <input type="password" name="j_password" />
            <button type="submit" name="_eventId_proceed">Login</button>
        </form>
        """

        # 3. SAMLResponse form
        r3 = MagicMock()
        r3.url = "https://lmuidp.lrz.de/idp/profile/SAML2/Redirect/SSO?execution=e1s2"
        r3.text = """
        <form action="https://moodle.lmu.de/Shibboleth.sso/SAML2/POST" method="post">
            <input type="hidden" name="SAMLResponse" value="PHNhbWxwOlJlc3BvbnNlPg==" />
            <input type="hidden" name="RelayState" value="cookie:12345" />
        </form>
        """

        # 4. Authenticated Moodle dashboard
        r4 = MagicMock()
        r4.url = "https://moodle.lmu.de/my/"
        r4.text = """
        <html>
        <script>var M = {"cfg": {"sesskey": "abc123sesskey", "userId": 9876}};</script>
        <span class="usertext">Paskal Tutor</span>
        </html>
        """

        session.get.return_value = r1
        session.post.side_effect = [r2, r3, r4]

        client = LMUMoodleClient(session=session)
        client.login("tutor@campus.lmu.de", "secret_password")

        self.assertEqual(client.sesskey, "abc123sesskey")
        self.assertEqual(client.userid, "9876")
        self.assertEqual(client.fullname, "Paskal Tutor")
        self.assertEqual(session.post.call_count, 3)


class TestCourseAndGradingTableParsing(unittest.TestCase):
    def test_find_assignment_cmid_from_course_html(self) -> None:
        course_html = """
        <li id="section-2" class="section main clearfix">
            <h3 class="sectionname">Übungsblätter</h3>
            <ul class="section img-text">
                <li class="activity assign modtype_assign">
                    <a class="aalink" href="https://moodle.lmu.de/mod/assign/view.php?id=2672310">
                        <span class="instancename">Übungsblatt 10<span class="accesshide "> Aufgabe</span></span>
                    </a>
                </li>
                <li class="activity assign modtype_assign">
                    <a class="aalink" href="https://moodle.lmu.de/mod/assign/view.php?id=2672327">
                        <span class="instancename">Übungsblatt 1<span class="accesshide "> Aufgabe</span></span>
                    </a>
                </li>
                <li class="activity assign modtype_assign">
                    <a class="aalink" href="https://moodle.lmu.de/mod/assign/view.php?id=2672999">
                        <span class="instancename">Übungsblatt 2<span class="accesshide "> Aufgabe</span></span>
                    </a>
                </li>
            </ul>
        </li>
        """
        cmid_1 = LMUMoodleClient.parse_assignment_cmid_from_course_html(course_html, 1)
        cmid_2 = LMUMoodleClient.parse_assignment_cmid_from_course_html(course_html, 2)
        cmid_10 = LMUMoodleClient.parse_assignment_cmid_from_course_html(course_html, 10)

        self.assertEqual(cmid_1, 2672327)
        self.assertEqual(cmid_2, 2672999)
        self.assertEqual(cmid_10, 2672310)

    def test_parse_grading_table_and_filter_by_bewerter(self) -> None:
        grading_html = """
        <table class="flexible generaltable gradingtable">
            <thead>
                <tr>
                    <th class="header c0">Auswahl</th>
                    <th class="header c1 fullname">Vorname / Nachname</th>
                    <th class="header c2">Status</th>
                    <th class="header c3 grade">Bewertung</th>
                    <th class="header c4 allocatedmarker">Bewerter</th>
                    <th class="header c5">Dateiabgaben</th>
                </tr>
            </thead>
            <tbody>
                <tr class="user101 r0">
                    <td class="c0"><input type="checkbox" name="selectedusers" value="101" /></td>
                    <td class="c1"><a href="https://moodle.lmu.de/user/view.php?id=101&course=48957">Max Mustermann</a></td>
                    <td class="c2">Zur Bewertung abgegeben (Teilnehmer/in 55501)</td>
                    <td class="c3"><input type="text" name="quickgrade_101" value="" /></td>
                    <td class="c4 allocatedmarker">Paskal Tutor</td>
                    <td class="c5">
                        <a href="https://moodle.lmu.de/pluginfile.php/99/assignsubmission_file/submission_files/101/blatt1.pdf?forcedownload=1">blatt1.pdf</a>
                    </td>
                </tr>
                <tr class="user102 r1">
                    <td class="c0"><input type="checkbox" name="selectedusers" value="102" /></td>
                    <td class="c1"><a href="https://moodle.lmu.de/user/view.php?id=102&course=48957">Erika Musterfrau</a></td>
                    <td class="c2">Zur Bewertung abgegeben (Teilnehmer/in 55502)</td>
                    <td class="c3"><input type="text" name="quickgrade_102" value="12,5" /></td>
                    <td class="c4 allocatedmarker">Other Tutor</td>
                    <td class="c5">
                        <a href="https://moodle.lmu.de/pluginfile.php/99/assignsubmission_file/submission_files/102/abgabe.pdf?forcedownload=1">abgabe.pdf</a>
                    </td>
                </tr>
                <tr class="user103 r0">
                    <td class="c0"><input type="checkbox" name="selectedusers" value="103" /></td>
                    <td class="c1"><a href="https://moodle.lmu.de/user/view.php?id=103&course=48957">Anna-Lena Müller</a></td>
                    <td class="c2">Zur Bewertung abgegeben (Teilnehmer/in 55503)</td>
                    <td class="c3"><input type="text" name="quickgrade_103" value="" /></td>
                    <td class="c4 allocatedmarker">
                        <select name="allocatedmarker_103">
                            <option value="9876" selected="selected">Tutor, Paskal</option>
                        </select>
                    </td>
                    <td class="c5">
                        <a href="https://moodle.lmu.de/pluginfile.php/99/assignsubmission_file/submission_files/103/p1.pdf?forcedownload=1">p1.pdf</a>
                    </td>
                </tr>
            </tbody>
        </table>
        """
        rows = LMUMoodleClient.parse_grading_table_html(grading_html)
        self.assertEqual(len(rows), 3)

        assigned = [
            r
            for r in rows
            if LMUMoodleClient._is_row_assigned_to_marker(r, "Paskal Tutor", "9876")
        ]
        self.assertEqual([r.userid for r in assigned], ["101", "103"])
        self.assertEqual(assigned[0].participant_id, "55501")
        self.assertEqual(
            assigned[0].moodle_folder, "Max Mustermann_55501_assignsubmission_file_"
        )


class TestPDFNamingAndPackaging(unittest.TestCase):
    def test_unified_naming_scheme(self) -> None:
        used: set[str] = set()
        f_jd, s_jd = split_fullname("john doe")
        name_jd = format_student_pdf_name(f_jd, s_jd, sheet_number=2, used_filenames=used, userid="100")
        self.assertEqual(name_jd, "JDoe_sheet2.pdf")

        f1, s1 = split_fullname("Max Mustermann")
        name1 = format_student_pdf_name(f1, s1, sheet_number=1, used_filenames=used, userid="101")
        self.assertEqual(name1, "MMustermann_sheet1.pdf")

        f2, s2 = split_fullname("Anna-Lena Müller")
        name2 = format_student_pdf_name(f2, s2, sheet_number=1, used_filenames=used, userid="103")
        self.assertEqual(name2, "AMueller_sheet1.pdf")

        # Duplicate student name gets disambiguated
        name1_dup = format_student_pdf_name(f1, s1, sheet_number=1, used_filenames=used, userid="109")
        self.assertEqual(name1_dup, "MMustermann_109_sheet1.pdf")

    def test_merge_multiple_student_pdfs(self) -> None:
        p1 = make_blank_pdf_bytes(num_pages=2)
        p2 = make_blank_pdf_bytes(num_pages=1)
        merged = merge_pdf_bytes_list([("page2.pdf", p2), ("page1.pdf", p1)])
        reader = PdfReader(io.BytesIO(merged))
        self.assertEqual(len(reader.pages), 3)

    def test_manifest_matching_and_feedback_zip_structure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            manifest_path = tmp_path / "manifest_sheet_1.json"
            csv_path = tmp_path / "grades_sheet_1.csv"
            marked_dir = tmp_path / "marked"
            marked_dir.mkdir()

            records = [
                StudentSubmissionRecord(
                    userid="101",
                    fullname="Max Mustermann",
                    firstname="Max",
                    surname="Mustermann",
                    filename="MMustermann_sheet1.pdf",
                    sheet_number=1,
                    participant_id="55501",
                    moodle_folder="Max Mustermann_55501_assignsubmission_file_",
                    original_filenames=["blatt1.pdf"],
                ),
                StudentSubmissionRecord(
                    userid="103",
                    fullname="Anna-Lena Müller",
                    firstname="Anna-Lena",
                    surname="Müller",
                    filename="AMueller_sheet1.pdf",
                    sheet_number=1,
                    participant_id="55503",
                    moodle_folder="Anna-Lena Müller_55503_assignsubmission_file_",
                    original_filenames=["p1.pdf"],
                ),
            ]

            save_manifest(manifest_path, 48957, 2672327, 1, records)
            sync_grades_csv(csv_path, records)

            # Simulate Notability export: one with score suffix, one exact name
            pdf_bytes = make_blank_pdf_bytes(1)
            (marked_dir / "MMustermann_sheet1_14.5p.pdf").write_bytes(pdf_bytes)
            (marked_dir / "AMueller_sheet1.pdf").write_bytes(pdf_bytes)

            course_id, cmid, sheet_num, loaded_students = load_manifest(manifest_path)
            self.assertEqual(course_id, 48957)
            self.assertEqual(cmid, 2672327)
            self.assertEqual(sheet_num, 1)

            matched = match_marked_pdfs(marked_dir, loaded_students, read_grades_csv(csv_path))
            self.assertEqual(len(matched), 2)
            # Sorted alphabetically by submitting student: Anna-Lena Müller first, Max Mustermann second
            self.assertEqual(matched[0].record.fullname, "Anna-Lena Müller")
            self.assertIsNone(matched[0].points)
            self.assertEqual(matched[1].record.fullname, "Max Mustermann")
            self.assertEqual(matched[1].points, "14.5")

            zip_bytes = build_moodle_feedback_zip(matched)
            with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
                names = set(zf.namelist())
                self.assertIn(
                    "Max Mustermann_55501_assignsubmission_file_/MMustermann_sheet1_annotated.pdf",
                    names,
                )
                self.assertIn(
                    "Anna-Lena Müller_55503_assignsubmission_file_/AMueller_sheet1_annotated.pdf",
                    names,
                )

    def test_partner_detection_and_dual_grading_expansion(self) -> None:
        roster = [
            CourseStudent(
                userid="101",
                fullname="Max Mustermann",
                firstname="Max",
                surname="Mustermann",
                participant_id="55501",
                moodle_folder="Max Mustermann_55501_assignsubmission_file_",
            ),
            CourseStudent(
                userid="102",
                fullname="Erika Musterfrau",
                firstname="Erika",
                surname="Musterfrau",
                participant_id="55502",
                moodle_folder="Erika Musterfrau_55502_assignsubmission_file_",
            ),
            CourseStudent(
                userid="103",
                fullname="John Doe",
                firstname="John",
                surname="Doe",
                participant_id="55503",
                moodle_folder="John Doe_55503_assignsubmission_file_",
            ),
        ]

        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "MMustermann_sheet1.pdf"
            pdf_path.write_bytes(
                make_blank_pdf_bytes(
                    1,
                    text="Analysis 1 Sheet 1 - Abgabe von Max Mustermann und Erika Musterfrau",
                )
            )
            rec = StudentSubmissionRecord(
                userid="101",
                fullname="Max Mustermann",
                firstname="Max",
                surname="Mustermann",
                filename="MMustermann_sheet1.pdf",
                sheet_number=1,
                participant_id="55501",
                moodle_folder="Max Mustermann_55501_assignsubmission_file_",
                original_filenames=["blatt1.pdf"],
            )
            partner = detect_partner_for_record(rec, pdf_path, roster)
            self.assertIsNotNone(partner)
            assert partner is not None
            self.assertEqual(partner.userid, "102")
            self.assertEqual(rec.partner_fullname, "Erika Musterfrau")

            # Expand marked item so both Max Mustermann (101) and Erika Musterfrau (102) receive the grade & PDF
            primary_item = MarkedSubmissionItem(record=rec, pdf_path=pdf_path, points="18.5")
            expanded = expand_marked_items_with_partners([primary_item], roster)
            self.assertEqual(len(expanded), 2)
            self.assertEqual(expanded[0].record.userid, "101")
            self.assertEqual(expanded[1].record.userid, "102")
            self.assertEqual(expanded[1].points, "18.5")
            self.assertEqual(
                expanded[1].record.moodle_folder,
                "Erika Musterfrau_55502_assignsubmission_file_",
            )

            # Verify feedback ZIP contains entries for both students in the pair
            zip_bytes = build_moodle_feedback_zip(expanded)
            with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
                names = set(zf.namelist())
                self.assertIn(
                    "Max Mustermann_55501_assignsubmission_file_/MMustermann_sheet1_annotated.pdf",
                    names,
                )
                self.assertIn(
                    "Erika Musterfrau_55502_assignsubmission_file_/MMustermann_sheet1_annotated.pdf",
                    names,
                )

    def test_noisy_handwriting_ocr_and_8digit_matriculation_minimum_distance(self) -> None:
        roster = [
            CourseStudent(userid="101", fullname="Max Mustermann"),
            CourseStudent(userid="102", fullname="Erika Musterfrau"),
            CourseStudent(userid="103", fullname="John Doe"),
            CourseStudent(userid="104", fullname="Lukas Lindemann"),
            CourseStudent(userid="105", fullname="Sophie Cauchy"),
        ]

        # Simulate noisy OCR at the top of a handwritten GoodNotes PDF where:
        # - Two 8-digit matriculation numbers have OCR character errors (1234S678 and 8765432I)
        # - Partner name "Erika Musterfrau" is misread by OCR as "Er1ka Mvstorfrau"
        # - Body of the page mentions "Satz von Cauchy" and "Lindemann", which should NOT beat the 8-digit anchor window
        noisy_ocr_header = (
            "Übungsblatt 2 - Analysis 1 - Tutor: Paskal\n"
            "Max Mustermann (Matr.-Nr. 1234S678)\n"
            "Er1ka Mvstorfrau (Matr.-Nr. 8765432I)\n"
        )
        anchors = find_matriculation_anchors(noisy_ocr_header)
        detected_nums = [a[0] for a in anchors if len(a[0]) == 8]
        self.assertIn("12345678", detected_nums)
        self.assertIn("87654321", detected_nums)

        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "MMustermann_sheet2.pdf"
            # Template body text has math names that should not override the top header + 8-digit anchor
            pdf_path.write_bytes(
                make_blank_pdf_bytes(
                    1,
                    text="Aufgabe 1: Verwenden Sie das Kriterium von Cauchy und Satz von Lindemann.",
                )
            )
            rec = StudentSubmissionRecord(
                userid="101",
                fullname="Max Mustermann",
                firstname="Max",
                surname="Mustermann",
                filename="MMustermann_sheet2.pdf",
                sheet_number=2,
                original_filenames=["abgabe2.pdf"],
            )
            partner = detect_partner_for_record(
                rec,
                pdf_path,
                roster,
                extra_top_text=noisy_ocr_header,
                marker_name="Paskal Tutor",
            )
            self.assertIsNotNone(partner)
            assert partner is not None
            self.assertEqual(partner.userid, "102")
            self.assertEqual(rec.partner_fullname, "Erika Musterfrau")
            self.assertIn("12345678", rec.detected_matriculations)
            self.assertIn("87654321", rec.detected_matriculations)
            self.assertTrue(len(rec.partner_candidates) >= 2)

    def test_excluded_professor_assistant_names_and_totally_typed_page_skipping(self) -> None:
        clear_excluded_partner_names()
        register_excluded_partner_names(
            ["Prof. Dr. Mock Lecturer", "Assistant Alpha", "Assistant Beta"]
        )

        self.assertTrue(is_excluded_partner_name("Prof. Dr. Mock Lecturer"))
        self.assertTrue(is_excluded_partner_name("Mock Lecturer"))
        self.assertTrue(is_excluded_partner_name("Lecturer Mock"))
        self.assertTrue(is_excluded_partner_name("Assistant Alpha"))
        self.assertTrue(is_excluded_partner_name("Assistant Beta"))
        self.assertFalse(is_excluded_partner_name("Anna-Lena Student"))
        self.assertFalse(is_excluded_partner_name("Erika Musterfrau"))

        roster = [
            CourseStudent(userid="1", fullname="Prof. Dr. Mock Lecturer"),
            CourseStudent(userid="2", fullname="Assistant Alpha"),
            CourseStudent(userid="3", fullname="Assistant Beta"),
            CourseStudent(userid="101", fullname="Max Mustermann"),
            CourseStudent(userid="102", fullname="Erika Musterfrau"),
            CourseStudent(userid="103", fullname="John Doe"),
        ]

        # 1. Verify a totally typed single-page assignment sheet is excluded from partner search
        typed_page_bytes = make_blank_pdf_bytes(
            1,
            text="Analysis 1 - Prof. Dr. Mock Lecturer, Assistant Alpha, Assistant Beta - Aufgabe 1: John Doe",
            totally_typed=True,
        )
        reader_typed = PdfReader(io.BytesIO(typed_page_bytes))
        self.assertTrue(is_totally_typed_page(reader_typed.pages[0]))

        with tempfile.TemporaryDirectory() as tmp:
            typed_only_path = Path(tmp) / "typed_only.pdf"
            typed_only_path.write_bytes(typed_page_bytes)
            txt_t, method_t = extract_text_and_ocr_from_pdf(typed_only_path)
            self.assertEqual(txt_t, "")
            self.assertEqual(method_t, "typed_excluded")

            # 2. Verify a 2-page PDF where Page 1 is the totally typed assignment sheet (mentioning
            # Prof. Dr. Mock Lecturer, Assistant Alpha, Assistant Beta, and John Doe) and Page 2 is the student's
            # handwritten page ("Max Mustermann 12345678, Erika Musterfrau 87654321") skips Page 1
            # and matches Erika Musterfrau (102) from Page 2
            handwritten_page2_bytes = make_blank_pdf_bytes(
                1,
                text="Prof. Dr. Mock Lecturer Assistant Alpha Assistant Beta Max Mustermann 12345678 Erika Musterfrau 87654321",
                totally_typed=False,
            )
            combined_bytes = merge_pdf_bytes_list(
                [("page1_assign.pdf", typed_page_bytes), ("page2_sol.pdf", handwritten_page2_bytes)]
            )
            merged_path = Path(tmp) / "MMustermann_sheet1.pdf"
            merged_path.write_bytes(combined_bytes)

            rec = StudentSubmissionRecord(
                userid="101",
                fullname="Max Mustermann",
                firstname="Max",
                surname="Mustermann",
                filename="MMustermann_sheet1.pdf",
                sheet_number=1,
            )
            partner = detect_partner_for_record(rec, merged_path, roster)
            self.assertIsNotNone(partner)
            assert partner is not None
            self.assertEqual(partner.userid, "102")
            self.assertEqual(rec.partner_fullname, "Erika Musterfrau")
            for cand_str in rec.partner_candidates:
                self.assertNotIn("Mock Lecturer", cand_str)
                self.assertNotIn("Assistant Alpha", cand_str)
                self.assertNotIn("Assistant Beta", cand_str)

    def test_moodle_trainer_role_recognition_from_participants(self) -> None:
        """Verify automatic recognition and exclusion of participants with trainer roles."""
        html = """
        <table class="generaltable" id="participants">
            <tbody>
                <tr>
                    <td><a href="https://moodle.lmu.de/user/view.php?id=10">Prof. Dr. Test Lecturer</a></td>
                    <td class="roles">Trainer/in</td>
                </tr>
                <tr>
                    <td><a href="https://moodle.lmu.de/user/view.php?id=11">Assistant First</a></td>
                    <td data-field="roles">Trainer/in ohne Bearbeitungsrecht</td>
                </tr>
                <tr>
                    <td><a href="https://moodle.lmu.de/user/view.php?id=12">Assistant Second</a></td>
                    <td class="cell c4 roles">Non-editing teacher</td>
                </tr>
                <tr>
                    <td><a href="https://moodle.lmu.de/user/view.php?id=101">Student Max</a></td>
                    <td class="roles">Student/in</td>
                </tr>
            </tbody>
        </table>
        """
        trainers = extract_trainers_from_participants_html(html)
        self.assertIn("Prof. Dr. Test Lecturer", trainers)
        self.assertIn("Assistant First", trainers)
        self.assertIn("Assistant Second", trainers)
        self.assertNotIn("Student Max", trainers)

        clear_excluded_partner_names()
        register_excluded_partner_names(trainers)
        self.assertTrue(is_excluded_partner_name("Prof. Dr. Test Lecturer"))
        self.assertTrue(is_excluded_partner_name("Test Lecturer"))
        self.assertTrue(is_excluded_partner_name("Assistant First"))
        self.assertFalse(is_excluded_partner_name("Student Max"))

    def test_submit_grades_and_upload_feedback_mock(self) -> None:
        session = MagicMock()
        client = LMUMoodleClient(session=session)
        client.sesskey = "sess_xyz"
        client.userid = "9876"
        client.fullname = "Paskal Tutor"

        # Page 1 has quickgrade_101, while partner 102 is graded via fallback single-student grade page
        grading_page = MagicMock()
        grading_page.url = "https://moodle.lmu.de/mod/assign/view.php?id=2672327&action=grading"
        grading_page.text = """
        <form action="https://moodle.lmu.de/mod/assign/view.php" method="post">
            <input type="hidden" name="id" value="2672327" />
            <input type="hidden" name="sesskey" value="sess_xyz" />
            <input type="hidden" name="sendstudentnotifications" value="1" />
            <input type="text" name="quickgrade_101" value="" />
            <input type="hidden" name="grademodified_101" value="0" />
            <select name="quickgrade_workflowstate_101">
                <option value="released" selected="selected">Freigegeben</option>
            </select>
            <input type="submit" name="savequickgrades" value="Save" />
        </form>
        """
        single_grade_page = MagicMock()
        single_grade_page.url = "https://moodle.lmu.de/mod/assign/view.php?id=2672327&action=grade&rownum=0&userid=102"
        single_grade_page.text = """
        <form class="mform" action="https://moodle.lmu.de/mod/assign/view.php" method="post">
            <input type="hidden" name="id" value="2672327" />
            <input type="hidden" name="sesskey" value="sess_xyz" />
            <input type="text" name="grade" value="" />
            <select name="workflowstate">
                <option value="released" selected="selected">Freigegeben</option>
            </select>
            <input type="submit" name="savegrade" value="Save changes" />
        </form>
        """

        save_resp = MagicMock()
        save_resp.url = "https://moodle.lmu.de/mod/assign/view.php"
        session.get.side_effect = [grading_page, single_grade_page]
        session.post.return_value = save_resp

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "MMustermann_sheet1.pdf"
            p.write_bytes(make_blank_pdf_bytes(1))
            rec1 = StudentSubmissionRecord(
                userid="101",
                fullname="Max Mustermann",
                firstname="Max",
                surname="Mustermann",
                filename="MMustermann_sheet1.pdf",
                sheet_number=1,
                participant_id="55501",
                moodle_folder="Max Mustermann_55501_assignsubmission_file_",
            )
            rec2 = StudentSubmissionRecord(
                userid="102",
                fullname="Erika Musterfrau",
                firstname="Erika",
                surname="Musterfrau",
                filename="MMustermann_sheet1.pdf",
                sheet_number=1,
                participant_id="55502",
                moodle_folder="Erika Musterfrau_55502_assignsubmission_file_",
            )
            item1 = MarkedSubmissionItem(record=rec1, pdf_path=p, points="14.5")
            item2 = MarkedSubmissionItem(record=rec2, pdf_path=p, points="14.5")
            count = client.submit_grades_to_grading_table(2672327, [item1, item2])
            self.assertEqual(count, 2)

            # First POST is quickgrade table for 101
            posted_table = session.post.call_args_list[0].kwargs["data"]
            self.assertEqual(posted_table["quickgrade_101"], "14,5")
            self.assertEqual(posted_table["sendstudentnotifications"], "0")
            self.assertEqual(posted_table["quickgrade_workflowstate_101"], "inmarking")

            # Second POST is direct single-student grade form for partner 102
            posted_single = session.post.call_args_list[1].kwargs["data"]
            self.assertEqual(posted_single["grade"], "14,5")
            self.assertEqual(posted_single["sendstudentnotifications"], "0")
            self.assertEqual(posted_single["workflowstate"], "inmarking")

    def test_upload_single_student_feedback_pdf_via_uploadfiles_and_grader(self) -> None:
        session = MagicMock()
        client = LMUMoodleClient(session=session)
        client.sesskey = "sess_xyz"
        client.userid = "9876"
        client.fullname = "Paskal Tutor"

        # Mock draft upload response
        draft_upload_resp = MagicMock()
        draft_upload_resp.json.return_value = {"itemid": 999888, "eventtype": "upload"}

        # 1. Batch uploadfiles page with files_101_filemanager
        batch_page = MagicMock()
        batch_page.url = "https://moodle.lmu.de/mod/assign/view.php"
        batch_page.text = """
        <form class="mform" action="/mod/assign/view.php" method="post">
            <input type="hidden" name="id" value="2672327" />
            <input type="hidden" name="sesskey" value="sess_xyz" />
            <input type="hidden" name="files_101_filemanager" value="999888" />
            <script>var opts = {"contextid": 12345, "itemid": 999888};</script>
            <script>var repos = [{"id": 5, "type": "upload"}];</script>
            <input type="submit" name="submitbutton" value="Save changes" />
        </form>
        """

        save_resp = MagicMock()
        save_resp.url = "https://moodle.lmu.de/mod/assign/view.php"

        session.get.return_value = batch_page
        session.post.side_effect = [draft_upload_resp, save_resp]

        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "MMustermann_sheet1.pdf"
            pdf_path.write_bytes(make_blank_pdf_bytes(1))
            rec = StudentSubmissionRecord(
                userid="101",
                fullname="Max Mustermann",
                firstname="Max",
                surname="Mustermann",
                filename="MMustermann_sheet1.pdf",
                sheet_number=1,
            )
            item = MarkedSubmissionItem(record=rec, pdf_path=pdf_path)
            success = client.upload_single_student_feedback_pdf(2672327, item)
            self.assertTrue(success)

            # Check draft upload call
            upload_call = session.post.call_args_list[0]
            self.assertIn("repository_ajax.php", upload_call.args[0])
            self.assertEqual(upload_call.kwargs["data"]["itemid"], "999888")
            self.assertEqual(upload_call.kwargs["data"]["ctx_id"], "12345")

            # Check form submit call
            submit_call = session.post.call_args_list[1]
            self.assertEqual(submit_call.kwargs["data"]["files_101_filemanager"], "999888")
            self.assertEqual(submit_call.kwargs["data"]["sendstudentnotifications"], "0")

    def test_upload_draft_file_recovers_from_repository_user_error(self) -> None:
        session = MagicMock()
        client = LMUMoodleClient(session=session)
        client.sesskey = "sess_xyz"

        # Simulate first response failing with repository_user::upload error, second succeeding
        resp_fail = MagicMock()
        resp_fail.json.return_value = {
            "error": "Fehler: Call to undefined method repository_user::upload()"
        }
        resp_success = MagicMock()
        resp_success.json.return_value = {"itemid": 12345, "eventtype": "upload"}

        session.post.side_effect = [resp_fail, resp_success]

        res = client.upload_draft_file(
            filename="annotated.pdf",
            file_bytes=b"%PDF-1.4",
            itemid="12345",
            ctx_id="67890",
            repo_id="5",
        )
        self.assertEqual(res.get("itemid"), 12345)
        # Verify it automatically switched from failing repo 5 to candidate repo 4
        self.assertEqual(client.upload_repo_id, "4")
        self.assertEqual(session.post.call_count, 2)
        second_call_data = session.post.call_args_list[1].kwargs["data"]
        self.assertEqual(second_call_data["repo_id"], "4")

    def test_find_upload_repo_id_from_moodle_boost_theme_html(self) -> None:
        client = LMUMoodleClient()
        html = """
        <script>
        var M = {
            "repositories": {
                "3": {"id": 3, "name": "Neueste Dateien", "type": "recent"},
                "4": {"id": 4, "name": "Datei hochladen", "icon": "https://moodle.lmu.de/icon.png", "type": "upload"},
                "5": {"id": 5, "name": "Meine Dateien", "type": "user"}
            }
        };
        </script>
        """
        extracted = client._find_upload_repo_id(html)
        self.assertEqual(extracted, "4")

    def test_fetch_participant_id_map_from_worksheet(self) -> None:
        session = MagicMock()
        client = LMUMoodleClient(session=session)
        client.sesskey = "sess_xyz"

        csv_resp = MagicMock()
        csv_resp.ok = True
        csv_resp.headers = {"Content-Type": "text/csv; charset=utf-8"}
        csv_resp.text = (
            "Identifier,Full name,Email address,Status,Grade\n"
            "Participant 55501,Max Mustermann,max@campus.lmu.de,Submitted,14.0\n"
            "Participant 55502,Erika Musterfrau,erika@campus.lmu.de,Submitted,\n"
        )
        session.get.return_value = csv_resp

        mapping = client.fetch_participant_id_map(2672327)
        self.assertEqual(mapping.get("max mustermann"), "55501")
        self.assertEqual(mapping.get("erika musterfrau"), "55502")

    def test_fetch_full_course_roster_merges_worksheet_and_participants(self) -> None:
        session = MagicMock()
        client = LMUMoodleClient(session=session)
        client.sesskey = "sess_xyz"
        client.last_all_rows = [
            RawSubmissionRow(
                userid="101",
                fullname="Max Mustermann",
                participant_id="55501",
                moodle_folder="Max Mustermann_55501_assignsubmission_file_",
            )
        ]

        unfiltered_grading = MagicMock()
        unfiltered_grading.ok = True
        unfiltered_grading.text = "<html></html>"

        csv_resp = MagicMock()
        csv_resp.ok = True
        csv_resp.headers = {"Content-Type": "text/csv; charset=utf-8"}
        csv_resp.text = (
            "Identifier,Full name,ID number,Status,Grade\n"
            "Participant 55501,Max Mustermann,12345678,Submitted,14.0\n"
            "Participant 55502,Erika Musterfrau,87654321,No submission,\n"
        )

        participants_resp = MagicMock()
        participants_resp.ok = True
        participants_resp.text = """
        <table>
            <tr>
                <td><a href="https://moodle.lmu.de/user/view.php?id=102&course=48957">Erika Musterfrau</a></td>
                <td>87654321</td>
            </tr>
            <tr>
                <td><a href="https://moodle.lmu.de/user/view.php?id=103&course=48957">John Doe</a></td>
                <td>11223344</td>
            </tr>
        </table>
        """

        session.get.side_effect = [unfiltered_grading, csv_resp, participants_resp]
        roster_rows = client.fetch_full_course_roster(48957, 2672327)
        by_uid = {r.userid: r for r in roster_rows}
        self.assertIn("101", by_uid)
        self.assertIn("102", by_uid)
        self.assertIn("103", by_uid)
        self.assertEqual(by_uid["102"].participant_id, "55502")
        self.assertEqual(by_uid["102"].matriculation_number, "87654321")
        self.assertEqual(
            by_uid["102"].moodle_folder, "Erika Musterfrau_55502_assignsubmission_file_"
        )

    def test_non_submitting_unassigned_partner_gets_graded(self) -> None:
        session = MagicMock()
        client = LMUMoodleClient(session=session)
        client.sesskey = "sess_xyz"
        client.userid = "9876"
        client.fullname = "Paskal Tutor"

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "MMustermann_sheet1.pdf"
            p.write_bytes(make_blank_pdf_bytes())

            # Submitter (assigned to tutor, submitted PDF)
            rec_submitter = StudentSubmissionRecord(
                userid="101",
                fullname="Max Mustermann",
                firstname="Max",
                surname="Mustermann",
                filename="MMustermann_sheet1.pdf",
                sheet_number=1,
                partner_userid="102",
                partner_fullname="Erika Musterfrau",
                partner_match_source="confirmed",
            )
            # Course roster includes Erika Musterfrau, who handed in nothing and is not assigned to tutor
            course_roster = [
                CourseStudent(
                    userid="101",
                    fullname="Max Mustermann",
                    firstname="Max",
                    surname="Mustermann",
                ),
                CourseStudent(
                    userid="102",
                    fullname="Erika Musterfrau",
                    firstname="Erika",
                    surname="Musterfrau",
                ),
            ]

            primary_items = [
                MarkedSubmissionItem(record=rec_submitter, pdf_path=p, points="18.5")
            ]
            expanded = expand_marked_items_with_partners(primary_items, course_roster)
            self.assertEqual(len(expanded), 2)
            self.assertEqual(expanded[1].record.userid, "102")
            self.assertEqual(expanded[1].points, "18.5")
            self.assertEqual(expanded[1].record.partner_match_source, "partner_expansion")

            # Simulate quickgrade table containing only Max Mustermann (101) because Erika (102)
            # is not assigned to this tutor / on another page, followed by direct grading page for 102
            qg_page = MagicMock()
            qg_page.ok = True
            qg_page.url = "https://moodle.lmu.de/mod/assign/view.php?id=2672327&action=grading"
            qg_page.text = """
            <form action="https://moodle.lmu.de/mod/assign/view.php" method="post">
                <input type="hidden" name="id" value="2672327" />
                <input type="hidden" name="sesskey" value="sess_xyz" />
                <input type="text" name="quickgrade_101" value="" />
                <input type="hidden" name="grademodified_101" value="0" />
                <input type="submit" name="savequickgrades" value="Save all quick grading changes" />
            </form>
            """

            partner_grade_page = MagicMock()
            partner_grade_page.ok = True
            partner_grade_page.url = (
                "https://moodle.lmu.de/mod/assign/view.php?id=2672327&action=grade&rownum=0&userid=102"
            )
            partner_grade_page.text = """
            <form class="mform" action="https://moodle.lmu.de/mod/assign/view.php" method="post">
                <input type="hidden" name="id" value="2672327" />
                <input type="hidden" name="userid" value="102" />
                <input type="hidden" name="action" value="submitgrade" />
                <input type="hidden" name="sesskey" value="sess_xyz" />
                <input type="text" name="grade" value="" />
                <select name="allocatedmarker">
                    <option value="0" selected="selected">Keine Zuordnung</option>
                    <option value="9876">Paskal Tutor</option>
                </select>
                <select name="workflowstate">
                    <option value="notmarked" selected="selected">Not marked</option>
                    <option value="released">Released</option>
                </select>
                <input type="submit" name="savegrade" value="Save changes" />
            </form>
            """

            post_ok = MagicMock()
            post_ok.status_code = 200
            session.get.side_effect = [qg_page, partner_grade_page]
            session.post.return_value = post_ok

            graded_count = client.submit_grades_to_grading_table(2672327, expanded)
            self.assertEqual(graded_count, 2)
            self.assertEqual(session.post.call_count, 2)

            # First POST: quickgrade for submitting student 101 sets points and grader (9876)
            qg_post_data = session.post.call_args_list[0].kwargs["data"]
            self.assertEqual(qg_post_data["quickgrade_101"], "18,5")
            self.assertEqual(qg_post_data["quickgrade_allocatedmarker_101"], "9876")
            self.assertEqual(qg_post_data["sendstudentnotifications"], "0")

            # Second POST: single-student grade for non-submitting, unassigned partner 102 sets points and grader (9876)
            partner_post_data = session.post.call_args_list[1].kwargs["data"]
            self.assertEqual(partner_post_data["userid"], "102")
            self.assertEqual(partner_post_data["grade"], "18,5")
            self.assertEqual(partner_post_data["allocatedmarker"], "9876")
            self.assertEqual(partner_post_data["sendstudentnotifications"], "0")


class TestGUIAndSettings(unittest.TestCase):
    def test_settings_persistence_never_saves_password(self) -> None:
        from pdf_processor import load_user_settings, save_user_settings

        with tempfile.TemporaryDirectory() as tmp:
            settings_file = Path(tmp) / "settings.json"
            save_user_settings(
                {
                    "workdir": "~icloud/LMU_Grading",
                    "username": "tutor@campus.lmu.de",
                    "password": "should_never_be_saved",
                },
                settings_path=settings_file,
            )
            raw_text = settings_file.read_text(encoding="utf-8")
            self.assertNotIn("should_never_be_saved", raw_text)
            loaded = load_user_settings(settings_path=settings_file)
            self.assertEqual(loaded["workdir"], "~icloud/LMU_Grading")
            self.assertEqual(loaded["username"], "tutor@campus.lmu.de")
            self.assertNotIn("password", loaded)

    def test_local_gui_server_endpoints_and_csrf(self) -> None:
        import threading
        from http.server import ThreadingHTTPServer
        import requests
        from gui import GradingAppState, make_handler_class
        from pdf_processor import SETTINGS_FILE

        orig_settings_bytes = SETTINGS_FILE.read_bytes() if SETTINGS_FILE.exists() else None
        app_state = GradingAppState()
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler_class(app_state))
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{port}"
            r_home = requests.get(f"{base}/", timeout=5)
            self.assertEqual(r_home.status_code, 200)
            self.assertIn('<meta name="color-scheme" content="light dark">', r_home.text)
            self.assertIn("#00883A", r_home.text)
            self.assertIn('closedby="any"', r_home.text)
            self.assertIn('autocomplete="current-password"', r_home.text)
            self.assertIn("container-type: inline-size", r_home.text)
            self.assertIn("header-preview-scroll", r_home.text)
            self.assertIn("toggle-pdf-preview", r_home.text)

            # Request without CSRF token must fail with 403
            r_forbidden = requests.post(
                f"{base}/api/settings",
                json={"workdir": "./sheets"},
                timeout=5,
            )
            self.assertEqual(r_forbidden.status_code, 403)

            # Request with valid CSRF token succeeds and persists sheet=2, workdir, and show_pdf_preview across /api/state calls
            with tempfile.TemporaryDirectory() as tmp:
                r_ok = requests.post(
                    f"{base}/api/settings",
                    headers={"X-CSRF-Token": app_state.csrf_token},
                    json={"workdir": tmp, "sheet": 2, "course_id": 48957, "show_pdf_preview": False},
                    timeout=5,
                )
                self.assertEqual(r_ok.status_code, 200)
                data = r_ok.json()
                self.assertEqual(data["workdir"], tmp)
                self.assertEqual(data["sheet"], 2)
                self.assertFalse(data["show_pdf_preview"])

                r_state = requests.get(f"{base}/api/state", timeout=5)
                self.assertEqual(r_state.status_code, 200)
                state_data = r_state.json()
                self.assertEqual(state_data["sheet"], 2)
                self.assertEqual(state_data["workdir"], tmp)
                self.assertFalse(state_data["show_pdf_preview"])

                # Populate sheet_2 with unsorted records that have older saved points & partners
                sheet2_dir = Path(tmp) / "sheet_2"
                to_mark_dir = sheet2_dir / "to_mark"
                marked_dir = sheet2_dir / "marked"
                to_mark_dir.mkdir(parents=True, exist_ok=True)
                marked_dir.mkdir(parents=True, exist_ok=True)

                rec_z = StudentSubmissionRecord(
                    userid="105",
                    fullname="Zoe Zimmermann",
                    firstname="Zoe",
                    surname="Zimmermann",
                    filename="ZZimmermann_sheet2.pdf",
                    sheet_number=2,
                    participant_id="55505",
                    moodle_folder="Zoe Zimmermann_55505_assignsubmission_file_",
                    current_grade="10.0",
                    partner_userid="102",
                    partner_fullname="Erika Musterfrau",
                    partner_match_source="confirmed",
                )
                rec_a = StudentSubmissionRecord(
                    userid="101",
                    fullname="Anna Albers",
                    firstname="Anna",
                    surname="Albers",
                    filename="AAlbers_sheet2.pdf",
                    sheet_number=2,
                    participant_id="55501",
                    moodle_folder="Anna Albers_55501_assignsubmission_file_",
                    current_grade="12.0",
                    partner_userid="103",
                    partner_fullname="John Doe",
                    partner_match_source="confirmed",
                )
                roster = [
                    CourseStudent(userid="101", fullname="Anna Albers", participant_id="55501"),
                    CourseStudent(userid="102", fullname="Erika Musterfrau", participant_id="55502"),
                    CourseStudent(userid="103", fullname="John Doe", participant_id="55503"),
                    CourseStudent(userid="105", fullname="Zoe Zimmermann", participant_id="55505"),
                ]
                manifest_path = sheet2_dir / "manifest_sheet_2.json"
                csv_path = sheet2_dir / "grades_sheet_2.csv"
                save_manifest(manifest_path, 48957, 2672999, 2, [rec_z, rec_a], course_roster=roster)
                sync_grades_csv(csv_path, [rec_z, rec_a])

                (marked_dir / "AAlbers_sheet2.pdf").write_bytes(make_blank_pdf_bytes())
                (marked_dir / "ZZimmermann_sheet2.pdf").write_bytes(make_blank_pdf_bytes())

                # 1. Verify /api/state returns students sorted alphabetically (Anna Albers before Zoe Zimmermann)
                r_sorted = requests.get(f"{base}/api/state?sheet=2", timeout=5).json()
                self.assertEqual(
                    [s["fullname"] for s in r_sorted["students"]],
                    ["Anna Albers", "Zoe Zimmermann"],
                )

                # 2. Verify /api/save-points overwrites points & partners completely without keeping old values
                r_save = requests.post(
                    f"{base}/api/save-points",
                    headers={"X-CSRF-Token": app_state.csrf_token},
                    json={
                        "sheet": 2,
                        "points": {
                            "AAlbers_sheet2.pdf": "19.5",
                            "ZZimmermann_sheet2.pdf": "",  # Explicitly cleared old 10.0 grade!
                        },
                        "partners": {
                            "AAlbers_sheet2.pdf": "Erika Musterfrau (102)",
                            "ZZimmermann_sheet2.pdf": "",  # Explicitly cleared old partner!
                        },
                    },
                    timeout=5,
                )
                self.assertEqual(r_save.status_code, 200)
                after_save = r_save.json()["state"]["students"]
                self.assertEqual(after_save[0]["fullname"], "Anna Albers")
                self.assertEqual(after_save[0]["points"], "19.5")
                self.assertEqual(after_save[0]["partner_userid"], "102")
                self.assertEqual(after_save[0]["partner_fullname"], "Erika Musterfrau")

                self.assertEqual(after_save[1]["fullname"], "Zoe Zimmermann")
                self.assertEqual(after_save[1]["points"], "")
                self.assertEqual(after_save[1]["partner_userid"], "")
                self.assertEqual(after_save[1]["partner_fullname"], "")

                # 3. Verify dry-run upload verifies 2 submitting students and 3 students total
                r_upload_dry = requests.post(
                    f"{base}/api/upload",
                    headers={"X-CSRF-Token": app_state.csrf_token},
                    json={"sheet": 2, "dry_run": True},
                    timeout=5,
                )
                self.assertEqual(r_upload_dry.status_code, 200)
                dry_msg = r_upload_dry.json()["message"]
                self.assertIn("for 2 submitting student(s)", dry_msg)
                self.assertIn("3 student(s) (2 submitter(s) + 1 partner(s))", dry_msg)
        finally:
            if orig_settings_bytes is not None:
                SETTINGS_FILE.write_bytes(orig_settings_bytes)
            elif SETTINGS_FILE.exists():
                SETTINGS_FILE.unlink()
            server.shutdown()
            server.server_close()

    def test_non_submitting_partner_with_part_uid_and_sticky_markerfilter(self) -> None:
        session = MagicMock()
        client = LMUMoodleClient(session=session)
        client.sesskey = "sess_xyz"
        client.userid = "9876"
        client.fullname = "Paskal Tutor"

        # 1. Initial GET for tifirst=E&tilast=M has sticky markerfilter=9876, hiding unallocated partner Erika (102)
        filtered_em_page = MagicMock()
        filtered_em_page.ok = True
        filtered_em_page.url = "https://moodle.lmu.de/mod/assign/view.php?id=2672327&action=grading&tifirst=E&tilast=M&perpage=100"
        filtered_em_page.text = """
        <form action="https://moodle.lmu.de/mod/assign/view.php" method="post">
            <input type="hidden" name="id" value="2672327" />
            <input type="hidden" name="userid" value="9876" />
            <input type="hidden" name="action" value="saveoptions" />
            <input type="hidden" name="sesskey" value="sess_xyz" />
            <input type="hidden" name="_qf__mod_assign_grading_options_form" value="1" />
            <select name="filter"><option value="" selected="selected">Kein Filter</option></select>
            <select name="markerfilter"><option value="9876" selected="selected">Paskal Tutor</option></select>
            <select name="workflowfilter"><option value="" selected="selected">Kein Filter</option></select>
            <select name="perpage"><option value="100" selected="selected">100</option></select>
            <input type="checkbox" name="quickgrading" value="1" checked="checked" />
            <input type="submit" name="submitbutton" value="Speichern" />
        </form>
        <table class="generaltable"><tbody></tbody></table>
        """

        # 2. POST saveoptions redirects to page 1 (without tifirst/tilast)
        saveoptions_redirect_page = MagicMock()
        saveoptions_redirect_page.ok = True
        saveoptions_redirect_page.url = "https://moodle.lmu.de/mod/assign/view.php?id=2672327&action=grading"
        saveoptions_redirect_page.text = "<html><body>Page 1 A-Z</body></html>"

        # 3. Re-GET for tifirst=E&tilast=M now shows unallocated partner Erika Musterfrau (userid=102)
        unfiltered_em_page = MagicMock()
        unfiltered_em_page.ok = True
        unfiltered_em_page.url = "https://moodle.lmu.de/mod/assign/view.php?id=2672327&action=grading&tifirst=E&tilast=M&perpage=100"
        unfiltered_em_page.text = """
        <table class="generaltable">
            <tbody>
                <tr class="user102">
                    <td class="c1"><input type="checkbox" name="selectedusers" value="102" /></td>
                    <td class="c2"><a href="https://moodle.lmu.de/user/view.php?id=102&amp;course=48957">Erika Musterfrau</a></td>
                </tr>
            </tbody>
        </table>
        <form action="https://moodle.lmu.de/mod/assign/view.php" method="post">
            <input type="hidden" name="id" value="2672327" />
            <input type="hidden" name="sesskey" value="sess_xyz" />
            <input type="hidden" name="action" value="quickgrade" />
            <input type="text" name="quickgrade_102" value="" />
            <input type="hidden" name="grademodified_102" value="1700000000" />
            <input type="hidden" name="gradeattempt_102" value="0" />
            <select name="quickgrade_102_allocatedmarker">
                <option value="0" selected="selected">Keine Auswahl</option>
                <option value="9876">Paskal Tutor</option>
            </select>
            <select name="quickgrade_102_workflowstate">
                <option value="released" selected="selected">Freigegeben</option>
            </select>
            <input type="submit" name="savequickgrades" value="Alle Bewertungsänderungen speichern" />
        </form>
        """

        # 4. Main quickgrade table page for submitting student 101
        main_grading_page = MagicMock()
        main_grading_page.ok = True
        main_grading_page.url = "https://moodle.lmu.de/mod/assign/view.php?id=2672327&action=grading&tifirst=&tilast=&perpage=100"
        main_grading_page.text = """
        <form action="https://moodle.lmu.de/mod/assign/view.php" method="post">
            <input type="hidden" name="id" value="2672327" />
            <input type="hidden" name="sesskey" value="sess_xyz" />
            <input type="hidden" name="action" value="quickgrade" />
            <input type="text" name="quickgrade_101" value="" />
            <input type="hidden" name="grademodified_101" value="1700000000" />
            <select name="quickgrade_101_allocatedmarker">
                <option value="0" selected="selected">Keine Auswahl</option>
                <option value="9876">Paskal Tutor</option>
            </select>
            <select name="quickgrade_101_workflowstate">
                <option value="inmarking" selected="selected">In Bewertung</option>
            </select>
            <input type="submit" name="savequickgrades" value="Save" />
        </form>
        """

        # 5. Single-student grade form for partner 102 (has real _qf__mod_assign_grade_form_102 and useridlistid,
        #    and lacks <select name="allocatedmarker"> because tutor does not have mod/assign:manageallocations)
        partner_grade_page = MagicMock()
        partner_grade_page.ok = True
        partner_grade_page.url = "https://moodle.lmu.de/mod/assign/view.php?id=2672327&action=grade&rownum=0&userid=102"
        partner_grade_page.text = """
        <form class="mform" action="https://moodle.lmu.de/mod/assign/view.php" method="post">
            <input type="hidden" name="id" value="2672327" />
            <input type="hidden" name="userid" value="102" />
            <input type="hidden" name="rownum" value="0" />
            <input type="hidden" name="useridlistid" value="6951d3f999" />
            <input type="hidden" name="action" value="submitgrade" />
            <input type="hidden" name="sesskey" value="sess_xyz" />
            <input type="hidden" name="_qf__mod_assign_grade_form_102" value="1" />
            <input type="text" name="grade" value="" />
            <select name="workflowstate">
                <option value="released" selected="selected">Freigegeben</option>
            </select>
            <input type="submit" name="savegrade" value="Save changes" />
        </form>
        """

        generic_post_ok = MagicMock()
        generic_post_ok.ok = True
        generic_post_ok.url = "https://moodle.lmu.de/mod/assign/view.php"

        # Sequence of GETs:
        # 1) resolve_student_userid_on_moodle -> GET tifirst=E&tilast=M (filtered_em_page)
        # 2) _clear_sticky_grading_filters_if_needed -> re-GET tifirst=E&tilast=M (unfiltered_em_page)
        # 3) submit_grades_to_grading_table -> GET main_grading_page
        # 4) submit_single_student_grade Step 1 -> GET action=grade&userid=102 (partner_grade_page)
        # 5) submit_single_student_grade Step 2 -> GET tifirst=E&tilast=M (unfiltered_em_page)
        session.get.side_effect = [
            filtered_em_page,
            unfiltered_em_page,
            main_grading_page,
            partner_grade_page,
            unfiltered_em_page,
        ]
        session.post.side_effect = [
            saveoptions_redirect_page,
            generic_post_ok,
            generic_post_ok,
            generic_post_ok,
        ]

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "MMustermann_sheet1.pdf"
            p.write_bytes(make_blank_pdf_bytes(1))
            rec1 = StudentSubmissionRecord(
                userid="101",
                fullname="Max Mustermann",
                firstname="Max",
                surname="Mustermann",
                filename="MMustermann_sheet1.pdf",
                sheet_number=1,
                participant_id="55501",
                moodle_folder="Max Mustermann_55501_assignsubmission_file_",
                partner_userid="part_55502",
                partner_fullname="Erika Musterfrau",
                partner_match_source="confirmed",
            )
            roster = [
                CourseStudent(userid="101", fullname="Max Mustermann", participant_id="55501"),
                CourseStudent(userid="part_55502", fullname="Erika Musterfrau", participant_id="55502"),
            ]
            primary_item = MarkedSubmissionItem(record=rec1, pdf_path=p, points="18.5")
            all_items = expand_marked_items_with_partners([primary_item], roster)
            self.assertEqual(len(all_items), 2)
            self.assertEqual(all_items[1].record.userid, "part_55502")

            graded_count = client.submit_grades_to_grading_table(2672327, all_items)
            self.assertEqual(graded_count, 2)
            # Partner's temporary "part_55502" userid was resolved to numeric "102"
            self.assertEqual(all_items[1].record.userid, "102")

            # Verify POST #1: saveoptions cleared markerfilter and sent submitbutton + quickgrading=1
            post_saveopts = session.post.call_args_list[0].kwargs["data"]
            self.assertEqual(post_saveopts["action"], "saveoptions")
            self.assertEqual(post_saveopts["markerfilter"], "")
            self.assertEqual(post_saveopts["quickgrading"], "1")
            self.assertEqual(post_saveopts["submitbutton"], "Speichern")

            # Verify POST #2: main quickgrade table for submitting student 101
            post_main_qg = session.post.call_args_list[1].kwargs["data"]
            self.assertEqual(post_main_qg["quickgrade_101"], "18,5")
            self.assertEqual(post_main_qg["quickgrade_101_allocatedmarker"], "9876")
            self.assertEqual(post_main_qg["sendstudentnotifications"], "0")

            # Verify POST #3: single-student grade form for partner 102 (clears useridlistid so rownum=0 isn't used)
            post_partner_single = session.post.call_args_list[2].kwargs["data"]
            self.assertEqual(post_partner_single["userid"], "102")
            self.assertEqual(post_partner_single["useridlistid"], "")
            self.assertEqual(post_partner_single["action"], "submitgrade")
            self.assertEqual(post_partner_single["grade"], "18,5")
            self.assertEqual(post_partner_single["workflowstate"], "inmarking")

            # Verify POST #4: follow-up quickgrade for partner 102 sets quickgrade_102_allocatedmarker=9876
            post_partner_qg = session.post.call_args_list[3].kwargs["data"]
            self.assertEqual(post_partner_qg["quickgrade_102"], "18,5")
            self.assertEqual(post_partner_qg["quickgrade_102_allocatedmarker"], "9876")
            self.assertEqual(post_partner_qg["quickgrade_102_workflowstate"], "inmarking")
            self.assertEqual(post_partner_qg["sendstudentnotifications"], "0")

    def test_moodle41_gradeform_class_and_setbatchmarkingallocation(self) -> None:
        """Verify Moodle 4.1 <form class='gradeform'>, files_filemanager, and setbatchmarkingallocation."""
        session = MagicMock()
        client = LMUMoodleClient(session=session)
        client.sesskey = "sess_xyz"
        client.userid = "9876"
        client.fullname = "Paskal Tutor"

        # 1. Moodle 4.1 single-student grade page uses class="gradeform" (not class="mform")
        moodle41_grade_page = MagicMock()
        moodle41_grade_page.ok = True
        moodle41_grade_page.url = "https://moodle.lmu.de/mod/assign/view.php?id=2672327&action=grade&rownum=0&userid=102"
        moodle41_grade_page.text = """
        <form autocomplete="off" action="https://moodle.lmu.de/mod/assign/view.php" method="post" id="mform1_abc" class="gradeform">
            <input type="hidden" name="id" value="2672327" />
            <input type="hidden" name="userid" value="102" />
            <input type="hidden" name="rownum" value="0" />
            <input type="hidden" name="useridlistid" value="6951d3f999" />
            <input type="hidden" name="action" value="submitgrade" />
            <input type="hidden" name="sesskey" value="sess_xyz" />
            <input type="hidden" name="_qf__mod_assign_grade_form_102" value="1" />
            <input type="text" name="grade" value="" />
            <input type="submit" name="savegrade" value="Save changes" />
        </form>
        """

        # 2. First GET on quickgrade page for unallocated partner 102 is empty because assign::list_participants()
        #    filters by uf.allocatedmarker = 9876
        empty_qg_page = MagicMock()
        empty_qg_page.ok = True
        empty_qg_page.url = "https://moodle.lmu.de/mod/assign/view.php?id=2672327&action=grading&tifirst=E&tilast=M&perpage=100"
        empty_qg_page.text = """
        <form action="https://moodle.lmu.de/mod/assign/view.php" method="post">
            <input type="hidden" name="id" value="2672327" />
            <input type="hidden" name="sesskey" value="sess_xyz" />
            <input type="hidden" name="action" value="quickgrade" />
            <input type="submit" name="savequickgrades" value="Save" />
        </form>
        """

        # 3. After allocate_marker_to_users (setbatchmarkingallocation), re-GET shows partner 102 in quickgrade
        allocated_qg_page = MagicMock()
        allocated_qg_page.ok = True
        allocated_qg_page.url = empty_qg_page.url
        allocated_qg_page.text = """
        <form action="https://moodle.lmu.de/mod/assign/view.php" method="post">
            <input type="hidden" name="id" value="2672327" />
            <input type="hidden" name="sesskey" value="sess_xyz" />
            <input type="hidden" name="action" value="quickgrade" />
            <input type="text" name="quickgrade_102" value="17,0" />
            <input type="hidden" name="grademodified_102" value="1700000000" />
            <input type="hidden" name="quickgrade_102_allocatedmarker" value="9876" />
            <input type="submit" name="savequickgrades" value="Save" />
        </form>
        """

        post_ok = MagicMock()
        post_ok.ok = True
        post_ok.status_code = 200
        post_ok.url = "https://moodle.lmu.de/mod/assign/view.php"

        session.get.side_effect = [moodle41_grade_page, empty_qg_page, allocated_qg_page]
        session.post.return_value = post_ok

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "MMustermann_sheet1.pdf"
            p.write_bytes(make_blank_pdf_bytes(1))
            partner_rec = StudentSubmissionRecord(
                userid="102",
                fullname="Erika Musterfrau",
                firstname="Erika",
                surname="Musterfrau",
                filename="MMustermann_sheet1.pdf",
                sheet_number=1,
                partner_match_source="partner_expansion",
            )
            partner_item = MarkedSubmissionItem(record=partner_rec, pdf_path=p, points="17.0")
            ok = client.submit_single_student_grade(2672327, partner_item)
            self.assertTrue(ok)
            self.assertEqual(session.post.call_count, 3)

            # POST #1: submitgrade on <form class="gradeform">
            p1 = session.post.call_args_list[0].kwargs["data"]
            self.assertEqual(p1["action"], "submitgrade")
            self.assertEqual(p1["userid"], "102")
            self.assertEqual(p1["grade"], "17,0")

            # POST #2: setbatchmarkingallocation to assign tutor 9876 as Bewerter in {assign_user_flags}
            p2 = session.post.call_args_list[1].kwargs["data"]
            self.assertEqual(p2["action"], "setbatchmarkingallocation")
            self.assertEqual(p2["selectedusers"], "102")
            self.assertEqual(p2["allocatedmarker"], "9876")
            self.assertEqual(p2["_qf__mod_assign_batch_set_allocatedmarker_form"], "1")

            # POST #3: quickgrade confirmation with quickgrade_102 and quickgrade_102_allocatedmarker
            p3 = session.post.call_args_list[2].kwargs["data"]
            self.assertEqual(p3["action"], "quickgrade")
            self.assertEqual(p3["quickgrade_102"], "17,0")
            self.assertEqual(p3["quickgrade_102_allocatedmarker"], "9876")

    def test_moodle41_batchuploadfilesform_and_participants_userinitials(self) -> None:
        """Verify Moodle 4.1 files_filemanager in batchuploadfilesform.php and userinitials stripping on /user/index.php."""
        session = MagicMock()
        client = LMUMoodleClient(session=session)
        client.sesskey = "sess_xyz"
        client.userid = "9876"
        client.fullname = "Paskal Tutor"

        # 1. /user/index.php page with <span class="userinitials size-35">EM</span>Erika Musterfrau
        participants_page = MagicMock()
        participants_page.ok = True
        participants_page.text = """
        <table id="participants" class="generaltable">
            <tbody>
                <tr>
                    <td>
                        <a href="https://moodle.lmu.de/user/view.php?id=102&amp;course=48957" class="d-inline-block aabtn">
                            <span class="userinitials size-35">EM</span>Erika Musterfrau
                        </a>
                    </td>
                    <td>12345678</td>
                </tr>
            </tbody>
        </table>
        """
        empty_grading = MagicMock()
        empty_grading.ok = True
        empty_grading.text = "<table class='generaltable'><tbody></tbody></table>"

        # resolve_student_userid_on_moodle tries (E, M), (E, ''), ('', '') on action=grading, then falls back to /user/index.php
        session.get.side_effect = [empty_grading, empty_grading, empty_grading, participants_page]
        resolved = client.resolve_student_userid_on_moodle(2672327, "Erika Musterfrau", course_id=48957)
        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved.userid, "102")
        self.assertEqual(resolved.fullname, "Erika Musterfrau")
        self.assertEqual(resolved.matriculation_number, "12345678")

        # 2. Moodle 4.1 batchuploadfilesform.php uses name="files_filemanager" and selectedusers
        uploadfiles_page = MagicMock()
        uploadfiles_page.ok = True
        uploadfiles_page.url = "https://moodle.lmu.de/mod/assign/view.php"
        uploadfiles_page.text = """
        <form class="mform" action="https://moodle.lmu.de/mod/assign/view.php" method="post">
            <input type="hidden" name="id" value="2672327" />
            <input type="hidden" name="selectedusers" value="102" />
            <input type="hidden" name="action" value="viewpluginpage" />
            <input type="hidden" name="pluginaction" value="uploadfiles" />
            <input type="hidden" name="plugin" value="file" />
            <input type="hidden" name="pluginsubtype" value="assignfeedback" />
            <input type="hidden" name="sesskey" value="sess_xyz" />
            <input type="hidden" name="_qf__assignfeedback_file_batch_upload_files_form" value="1" />
            <input type="hidden" name="files_filemanager" value="777666" />
            <script>var cfg = {"contextid": 54321, "itemid": 777666, "repositories": {"4": {"id": 4, "type": "upload"}}};</script>
            <input type="submit" name="submitbutton" value="Save changes" />
        </form>
        """
        draft_resp = MagicMock()
        draft_resp.json.return_value = {"itemid": 777666, "eventtype": "upload"}
        save_resp = MagicMock()
        save_resp.ok = True
        save_resp.status_code = 200

        session.get.side_effect = [uploadfiles_page]
        session.post.side_effect = [draft_resp, save_resp]

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "MMustermann_sheet1.pdf"
            p.write_bytes(make_blank_pdf_bytes(1))
            partner_rec = StudentSubmissionRecord(
                userid="102",
                fullname="Erika Musterfrau",
                firstname="Erika",
                surname="Musterfrau",
                filename="MMustermann_sheet1.pdf",
                sheet_number=1,
            )
            item = MarkedSubmissionItem(record=partner_rec, pdf_path=p, points="17.0")
            uploaded = client.upload_single_student_feedback_pdf(2672327, item)
            self.assertTrue(uploaded)
            post_form = session.post.call_args_list[-1].kwargs["data"]
            self.assertEqual(post_form["files_filemanager"], "777666")
            self.assertEqual(post_form["selectedusers"], "102")

    def test_partner_and_submitting_student_graded_together_in_quickgrade(self) -> None:
        """Verify that submitting student and partner are both graded in the main quickgrade POST."""
        session = MagicMock()
        client = LMUMoodleClient(session=session)
        client.sesskey = "sess_xyz"
        client.userid = "9876"
        client.fullname = "Paskal Tutor"

        grading_page = MagicMock()
        grading_page.ok = True
        grading_page.url = "https://moodle.lmu.de/mod/assign/view.php?id=2672327&action=grading&tifirst=&tilast=&perpage=100"
        grading_page.text = """
        <form action="https://moodle.lmu.de/mod/assign/view.php" method="post">
            <input type="hidden" name="id" value="2672327" />
            <input type="hidden" name="sesskey" value="sess_xyz" />
            <input type="hidden" name="action" value="quickgrade" />
            <input type="text" name="quickgrade_101" value="" />
            <input type="hidden" name="grademodified_101" value="1600000000" />
            <input type="hidden" name="gradeattempt_101" value="0" />
            <select name="quickgrade_101_workflowstate">
                <option value="released" selected="selected">Freigegeben</option>
            </select>
            <input type="text" name="quickgrade_102" value="" />
            <input type="hidden" name="grademodified_102" value="1600000000" />
            <input type="hidden" name="gradeattempt_102" value="" />
            <select name="quickgrade_102_workflowstate">
                <option value="released" selected="selected">Freigegeben</option>
            </select>
            <textarea name="quickgrade_comments_101">Existing comment</textarea>
            <input type="button" name="cancelbutton" value="Cancel" />
            <input type="submit" name="savequickgrades" value="Speichern" />
        </form>
        """

        post_ok = MagicMock()
        post_ok.ok = True
        post_ok.status_code = 200
        post_ok.url = "https://moodle.lmu.de/mod/assign/view.php?id=2672327&action=quickgradingresult"

        session.get.return_value = grading_page
        session.post.return_value = post_ok

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "JDoe_sheet2.pdf"
            p.write_bytes(make_blank_pdf_bytes(1))
            rec1 = StudentSubmissionRecord(
                userid="101",
                fullname="John Doe",
                firstname="John",
                surname="Doe",
                filename="JDoe_sheet2.pdf",
                sheet_number=2,
                partner_userid="102",
                partner_fullname="Jane Smith",
                partner_match_source="confirmed",
            )
            rec2 = StudentSubmissionRecord(
                userid="102",
                fullname="Jane Smith",
                firstname="Jane",
                surname="Smith",
                filename="JDoe_sheet2.pdf",
                sheet_number=2,
                partner_match_source="partner_expansion",
            )
            item1 = MarkedSubmissionItem(record=rec1, pdf_path=p, points="20.0")
            item2 = MarkedSubmissionItem(record=rec2, pdf_path=p, points="20.0")

            graded_count = client.submit_grades_to_grading_table(2672327, [item1, item2])
            self.assertEqual(graded_count, 2)
            self.assertEqual(session.post.call_count, 1)

            post_data = session.post.call_args[1]["data"]
            self.assertEqual(post_data["action"], "quickgrade")
            self.assertEqual(post_data["quickgrade_101"], "20,0")
            self.assertEqual(post_data["quickgrade_102"], "20,0")
            self.assertEqual(post_data["quickgrade_101_allocatedmarker"], "9876")
            self.assertEqual(post_data["quickgrade_102_allocatedmarker"], "9876")
            self.assertEqual(post_data["quickgrade_101_workflowstate"], "inmarking")
            self.assertEqual(post_data["quickgrade_102_workflowstate"], "inmarking")
            self.assertEqual(post_data["sendstudentnotifications"], "0")
            self.assertEqual(post_data["quickgrade_comments_101"], "Existing comment")
            self.assertNotIn("cancelbutton", post_data)

    def test_is_student_graded_by_other(self) -> None:
        """Verify detection of student submissions already graded by another tutor."""
        client = LMUMoodleClient()
        client.fullname = "Paskal Tutor"
        client.userid = "9876"

        row_other_tutor = RawSubmissionRow(
            userid="102",
            fullname="Erika Musterfrau",
            allocated_marker="Other Tutor",
            allocated_marker_id="5555",
            current_grade="14,5",
        )
        is_graded, reason = client.is_student_graded_by_other(row_other_tutor)
        self.assertTrue(is_graded)
        self.assertIn("Other Tutor", reason)
        self.assertIn("14,5", reason)

        row_grader_other = RawSubmissionRow(
            userid="103",
            fullname="Bob Schmidt",
            allocated_marker="",
            allocated_marker_id="",
            current_grade="10,0",
            grader="Dr. Miller",
        )
        is_graded, reason = client.is_student_graded_by_other(row_grader_other)
        self.assertTrue(is_graded)
        self.assertIn("Dr. Miller", reason)

        row_my_student = RawSubmissionRow(
            userid="101",
            fullname="Max Mustermann",
            allocated_marker="Paskal Tutor",
            allocated_marker_id="9876",
            current_grade="18,0",
        )
        is_graded, reason = client.is_student_graded_by_other(row_my_student)
        self.assertFalse(is_graded)
        self.assertEqual(reason, "")

        row_ungraded = RawSubmissionRow(
            userid="104",
            fullname="Anna Meyer",
            allocated_marker="Other Tutor",
            allocated_marker_id="5555",
            current_grade="",
        )
        is_graded, reason = client.is_student_graded_by_other(row_ungraded)
        self.assertFalse(is_graded)

        row_dash_grade = RawSubmissionRow(
            userid="105",
            fullname="Carl Becker",
            allocated_marker="Other Tutor",
            current_grade="-",
        )
        is_graded, reason = client.is_student_graded_by_other(row_dash_grade)
        self.assertFalse(is_graded)

    def test_filter_items_graded_by_others(self) -> None:
        """Verify filtering out students already graded by another tutor while keeping others."""
        client = LMUMoodleClient()
        client.fullname = "Paskal Tutor"
        client.userid = "9876"

        row_max = RawSubmissionRow(
            userid="101",
            fullname="Max Mustermann",
            allocated_marker="Paskal Tutor",
            allocated_marker_id="9876",
            current_grade="",
        )
        row_erika = RawSubmissionRow(
            userid="102",
            fullname="Erika Musterfrau",
            allocated_marker="Other Tutor",
            allocated_marker_id="5555",
            current_grade="12,5",
        )
        client.last_all_rows = [row_max, row_erika]

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "sheet.pdf"
            p.write_bytes(make_blank_pdf_bytes(1))
            rec1 = StudentSubmissionRecord(
                userid="101",
                fullname="Max Mustermann",
                firstname="Max",
                surname="Mustermann",
                filename="max.pdf",
                sheet_number=1,
            )
            rec2 = StudentSubmissionRecord(
                userid="102",
                fullname="Erika Musterfrau",
                firstname="Erika",
                surname="Musterfrau",
                filename="max.pdf",
                sheet_number=1,
                partner_match_source="partner_expansion",
            )
            item1 = MarkedSubmissionItem(record=rec1, pdf_path=p, points="18.0")
            item2 = MarkedSubmissionItem(record=rec2, pdf_path=p, points="18.0")

            valid_items, skipped = client.filter_items_graded_by_others(
                cmid=2672327, items=[item1, item2]
            )
            self.assertEqual(len(valid_items), 1)
            self.assertEqual(valid_items[0].record.fullname, "Max Mustermann")
            self.assertEqual(len(skipped), 1)
            self.assertEqual(skipped[0][0].record.fullname, "Erika Musterfrau")
            self.assertIn("Other Tutor", skipped[0][1])
            self.assertIn("12,5", skipped[0][1])

    def test_upload_skips_partner_graded_by_someone_else_and_completes_rest(self) -> None:
        """Verify upload skips partner graded by someone else, alters nothing for them, and finishes the rest."""
        import json
        import threading
        from http.server import ThreadingHTTPServer
        import requests
        from gui import GradingAppState, make_handler_class
        from pdf_processor import SETTINGS_FILE, save_user_settings

        orig_settings = SETTINGS_FILE.read_bytes() if SETTINGS_FILE.exists() else None
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                sheet_dir = root / "sheet_1"
                marked_dir = sheet_dir / "marked"
                marked_dir.mkdir(parents=True)
                to_mark_dir = sheet_dir / "to_mark"
                to_mark_dir.mkdir(parents=True)

                pdf_file = marked_dir / "Mustermann_Max_blatt1.pdf"
                pdf_file.write_bytes(make_blank_pdf_bytes(1))

                grades_csv = sheet_dir / "grades_sheet_1.csv"
                grades_csv.write_text("Mustermann_Max_blatt1.pdf,19.5\n", encoding="utf-8")

                manifest_path = sheet_dir / "manifest_sheet_1.json"
                rec_max = StudentSubmissionRecord(
                    userid="101",
                    fullname="Max Mustermann",
                    firstname="Max",
                    surname="Mustermann",
                    filename="Mustermann_Max_blatt1.pdf",
                    sheet_number=1,
                    partner_userid="102",
                    partner_fullname="Erika Musterfrau",
                    partner_match_source="confirmed",
                    moodle_folder="Max Mustermann_55501_assignsubmission_file_",
                )
                roster = [
                    RawSubmissionRow(
                        userid="101",
                        fullname="Max Mustermann",
                        allocated_marker="Paskal Tutor",
                        allocated_marker_id="9876",
                        current_grade="",
                    ),
                    RawSubmissionRow(
                        userid="102",
                        fullname="Erika Musterfrau",
                        allocated_marker="Other Tutor",
                        allocated_marker_id="5555",
                        current_grade="14,0",
                    ),
                ]
                save_manifest(manifest_path, 48957, 2672999, 1, [rec_max], course_roster=roster)
                save_user_settings({"workdir": str(root), "last_sheet": 1, "course_id": 48957, "marker": "Paskal Tutor"})

                app_state = GradingAppState()
                app_state.client.sesskey = "test_sesskey"
                app_state.client.userid = "9876"
                app_state.client.fullname = "Paskal Tutor"
                app_state.client.last_all_rows = list(roster)

                allocated_uids_log = []
                def fake_allocate(cmid, uids, marker_id=None):
                    allocated_uids_log.extend(uids)
                    return True
                app_state.client.allocate_marker_to_users = MagicMock(side_effect=fake_allocate)

                uploaded_pdf_items = []
                def fake_upload_single(cmid, item):
                    uploaded_pdf_items.append(item.record.userid)
                    return True
                app_state.client.upload_single_student_feedback_pdf = MagicMock(side_effect=fake_upload_single)
                app_state.client.upload_bulk_feedback_zip = MagicMock(return_value=False)
                app_state.client.upload_batch_feedback_pdfs = MagicMock(side_effect=lambda cmid, items: (0, items))

                submitted_grade_items = []
                def fake_submit_grades(cmid, items):
                    submitted_grade_items.extend([it.record.userid for it in items])
                    return len(items)
                app_state.client.submit_grades_to_grading_table = MagicMock(side_effect=fake_submit_grades)

                server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler_class(app_state))
                port = server.server_address[1]
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    url = f"http://127.0.0.1:{port}/api/upload"
                    csrf_headers = {
                        "Content-Type": "application/json",
                        "X-CSRF-Token": app_state.csrf_token,
                    }
                    resp = requests.post(
                        url,
                        headers=csrf_headers,
                        data=json.dumps({"sheet": 1, "dry_run": False}),
                        timeout=5,
                    )
                    self.assertEqual(resp.status_code, 200)
                    resp_json = resp.json()
                    self.assertTrue(resp_json["ok"])
                    self.assertIn("Erika Musterfrau", resp_json["message"])
                    self.assertIn("Ignored 1 student(s)", resp_json["message"])
                    self.assertEqual(len(resp_json["skipped_students"]), 1)
                    self.assertEqual(resp_json["skipped_students"][0]["name"], "Erika Musterfrau")

                    # Verify Erika (102) was NEVER allocated, NEVER uploaded, and NEVER submitted
                    self.assertIn("101", allocated_uids_log)
                    self.assertNotIn("102", allocated_uids_log)
                    self.assertIn("101", uploaded_pdf_items)
                    self.assertNotIn("102", uploaded_pdf_items)
                    self.assertIn("101", submitted_grade_items)
                    self.assertNotIn("102", submitted_grade_items)
                finally:
                    server.shutdown()
                    server.server_close()
        finally:
            if orig_settings is not None:
                SETTINGS_FILE.write_bytes(orig_settings)
            elif SETTINGS_FILE.exists():
                SETTINGS_FILE.unlink()

    def test_quickgrade_optimistic_lock_conflict_falls_back_to_single_grade(self) -> None:
        """Verify that Quickgrade conflict error falls back to per-student grading without failing."""
        session = MagicMock()
        client = LMUMoodleClient(session=session)
        client.sesskey = "sess_xyz"
        client.userid = "9876"
        client.fullname = "Paskal Tutor"

        grading_page = MagicMock()
        grading_page.ok = True
        grading_page.url = "https://moodle.lmu.de/mod/assign/view.php?id=2672327&action=grading&tifirst=&tilast=&perpage=100"
        grading_page.text = """
        <form action="https://moodle.lmu.de/mod/assign/view.php" method="post">
            <input type="hidden" name="id" value="2672327" />
            <input type="hidden" name="sesskey" value="sess_xyz" />
            <input type="hidden" name="action" value="quickgrade" />
            <input type="text" name="quickgrade_101" value="" />
            <input type="hidden" name="grademodified_101" value="1600000000" />
            <input type="submit" name="savequickgrades" value="Speichern" />
        </form>
        """

        post_conflict = MagicMock()
        post_conflict.ok = True
        post_conflict.status_code = 200
        post_conflict.text = "Fehler: Die Bewertung wurde von einer anderen Person geändert (errorrecordmodified)."

        session.get.return_value = grading_page
        session.post.return_value = post_conflict

        client.submit_single_student_grade = MagicMock(return_value=True)

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "JDoe_sheet2.pdf"
            p.write_bytes(make_blank_pdf_bytes(1))
            rec1 = StudentSubmissionRecord(
                userid="101",
                fullname="John Doe",
                firstname="John",
                surname="Doe",
                filename="JDoe_sheet2.pdf",
                sheet_number=2,
            )
            item1 = MarkedSubmissionItem(record=rec1, pdf_path=p, points="17.0")

            graded_count = client.submit_grades_to_grading_table(2672327, [item1])
            self.assertEqual(graded_count, 1)
            client.submit_single_student_grade.assert_called_once_with(2672327, item1)

    def test_cli_upload_skips_partner_graded_by_someone_else(self) -> None:
        """Verify CLI upload filters out already-graded partner and completes upload."""
        import argparse
        from unittest.mock import patch
        from cli import cmd_upload
        from pdf_processor import SETTINGS_FILE, save_user_settings

        orig_settings = SETTINGS_FILE.read_bytes() if SETTINGS_FILE.exists() else None
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                sheet_dir = root / "sheet_1"
                marked_dir = sheet_dir / "marked"
                marked_dir.mkdir(parents=True)
                to_mark_dir = sheet_dir / "to_mark"
                to_mark_dir.mkdir(parents=True)

                pdf_file = marked_dir / "Mustermann_Max_blatt1.pdf"
                pdf_file.write_bytes(make_blank_pdf_bytes(1))

                grades_csv = sheet_dir / "grades_sheet_1.csv"
                grades_csv.write_text("Mustermann_Max_blatt1.pdf,19.5\n", encoding="utf-8")

                manifest_path = sheet_dir / "manifest_sheet_1.json"
                rec_max = StudentSubmissionRecord(
                    userid="101",
                    fullname="Max Mustermann",
                    firstname="Max",
                    surname="Mustermann",
                    filename="Mustermann_Max_blatt1.pdf",
                    sheet_number=1,
                    partner_userid="102",
                    partner_fullname="Erika Musterfrau",
                    partner_match_source="confirmed",
                    moodle_folder="Max Mustermann_55501_assignsubmission_file_",
                )
                roster = [
                    RawSubmissionRow(
                        userid="101",
                        fullname="Max Mustermann",
                        allocated_marker="Paskal Tutor",
                        allocated_marker_id="9876",
                        current_grade="",
                    ),
                    RawSubmissionRow(
                        userid="102",
                        fullname="Erika Musterfrau",
                        allocated_marker="Other Tutor",
                        allocated_marker_id="5555",
                        current_grade="14,0",
                    ),
                ]
                save_manifest(manifest_path, 48957, 2672999, 1, [rec_max], course_roster=roster)
                save_user_settings({"workdir": str(root), "last_sheet": 1, "course_id": 48957, "marker": "Paskal Tutor"})

                args = argparse.Namespace(
                    sheet=1,
                    course=48957,
                    workdir=str(root),
                    marked_dir=None,
                    no_prompt_points=True,
                    username="tutor",
                    dry_run=False,
                )

                allocated_uids = []
                uploaded_uids = []
                graded_uids = []

                with patch("cli.prompt_credentials_in_memory", return_value=("tutor", "secret")), \
                     patch("cli.LMUMoodleClient") as mock_client_cls:
                    mock_client = MagicMock()
                    mock_client_cls.return_value = mock_client
                    mock_client.sesskey = "sess_123"
                    mock_client.userid = "9876"
                    mock_client.fullname = "Paskal Tutor"
                    mock_client.last_all_rows = list(roster)

                    # Use real filter implementation
                    real_client = LMUMoodleClient()
                    real_client.fullname = "Paskal Tutor"
                    real_client.userid = "9876"
                    real_client.last_all_rows = list(roster)
                    mock_client.filter_items_graded_by_others.side_effect = real_client.filter_items_graded_by_others

                    def fake_alloc(cmid, uids, marker_id=None):
                        allocated_uids.extend(uids)
                        return True
                    mock_client.allocate_marker_to_users.side_effect = fake_alloc

                    def fake_upload_single(cmid, item):
                        uploaded_uids.append(item.record.userid)
                        return True
                    mock_client.upload_single_student_feedback_pdf.side_effect = fake_upload_single
                    mock_client.upload_bulk_feedback_zip.return_value = False
                    mock_client.upload_batch_feedback_pdfs.side_effect = lambda cmid, items: (0, items)

                    def fake_submit(cmid, items):
                        graded_uids.extend([it.record.userid for it in items])
                        return len(items)
                    mock_client.submit_grades_to_grading_table.side_effect = fake_submit

                    exit_code = cmd_upload(args)
                    self.assertEqual(exit_code, 0)

                    # Verify partner (102) was NOT touched
                    self.assertIn("101", allocated_uids)
                    self.assertNotIn("102", allocated_uids)
                    self.assertIn("101", uploaded_uids)
                    self.assertNotIn("102", uploaded_uids)
                    self.assertIn("101", graded_uids)
                    self.assertNotIn("102", graded_uids)
        finally:
            if orig_settings is not None:
                SETTINGS_FILE.write_bytes(orig_settings)
            elif SETTINGS_FILE.exists():
                SETTINGS_FILE.unlink()


if __name__ == "__main__":
    unittest.main()
