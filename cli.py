#!/usr/bin/env python3
"""Command-line tool to automate LMU Moodle exercise downloads, group partner OCR matching, and Notability uploads."""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from moodle_client import (
    DEFAULT_COURSE_ID,
    LMUMoodleClient,
    MoodleAuthError,
    MoodleScrapingError,
    SecurityError,
)
from pdf_processor import (
    CourseStudent,
    MarkedSubmissionItem,
    StudentSubmissionRecord,
    build_moodle_feedback_zip,
    detect_partner_for_record,
    expand_marked_items_with_partners,
    format_student_pdf_name,
    load_manifest,
    load_manifest_with_roster,
    load_user_settings,
    match_marked_pdfs,
    merge_pdf_bytes_list,
    read_grades_csv,
    resolve_storage_path,
    save_manifest,
    save_user_settings,
    split_fullname,
    sync_grades_csv,
)


def prompt_credentials_in_memory(username_arg: Optional[str] = None) -> Tuple[str, str]:
    """Read LMU Moodle username and password in memory without storing them on disk."""
    username = (username_arg or "").strip()
    if not username:
        if sys.stdin.isatty():
            username = input("LMU Username: ").strip()
        else:
            username = sys.stdin.readline().strip()

    if sys.stdin.isatty():
        password = getpass.getpass(
            "LMU Password (tap Passwords key on iPad keyboard to AutoFill): "
        )
    else:
        password = sys.stdin.readline().rstrip("\r\n")

    if not username or not password:
        raise MoodleAuthError("Both username and password are required.")
    return username, password


def sheet_paths(workdir: Path, sheet_number: int) -> Tuple[Path, Path, Path, Path, Path]:
    """Return (sheet_dir, to_mark_dir, marked_dir, manifest_path, grades_csv_path)."""
    sheet_dir = workdir / f"sheet_{sheet_number}"
    to_mark_dir = sheet_dir / "to_mark"
    marked_dir = sheet_dir / "marked"
    manifest_path = sheet_dir / f"manifest_sheet_{sheet_number}.json"
    grades_csv_path = sheet_dir / f"grades_sheet_{sheet_number}.csv"
    return sheet_dir, to_mark_dir, marked_dir, manifest_path, grades_csv_path


def cmd_download(args: argparse.Namespace) -> int:
    """Authenticate, filter submissions by Bewerter, and save [FirstInitial][Lastname]_sheet[N].pdf."""
    settings = load_user_settings()
    sheet_num: int = args.sheet
    course_id: int = args.course_id
    workdir_str = args.workdir or str(settings.get("workdir") or "./sheets")
    workdir = resolve_storage_path(workdir_str)
    sheet_dir, to_mark_dir, marked_dir, manifest_path, grades_csv_path = sheet_paths(
        workdir, sheet_num
    )

    username, password = prompt_credentials_in_memory(
        args.username or str(settings.get("username") or "")
    )
    save_user_settings(
        {
            "workdir": workdir_str,
            "course_id": course_id,
            "last_sheet": sheet_num,
            "username": username,
        }
    )
    client = LMUMoodleClient()
    print("Authenticating with lmuidp.lrz.de and moodle.lmu.de...")
    client.login(username=username, password=password)
    del password

    print(
        f"Logged in as {client.fullname or username} (userid={client.userid or 'unknown'})."
    )

    cmid = args.cmid
    if not cmid:
        print(f"Locating 'Übungsblatt {sheet_num}' in course {course_id}...")
        cmid = client.find_assignment_cmid(course_id=course_id, sheet_number=sheet_num)
    print(f"Target assignment: Übungsblatt {sheet_num} (cmid={cmid}).")

    print("Fetching grading table and filtering by column 'Bewerter'...")
    assigned_rows = client.fetch_assigned_submissions(
        cmid=cmid, marker_query=args.marker
    )
    if not assigned_rows:
        print(
            "No rows matched your name in the 'Bewerter' column. "
            "If Moodle lists your name differently, pass --marker 'Your Name'."
        )
        return 1

    all_course_rows = client.fetch_full_course_roster(course_id=course_id, cmid=cmid)
    part_by_name = {
        r.fullname.lower(): (r.participant_id, r.moodle_folder)
        for r in all_course_rows
        if r.participant_id
    }
    for r in assigned_rows:
        if not r.participant_id and r.fullname.lower() in part_by_name:
            r.participant_id, r.moodle_folder = part_by_name[r.fullname.lower()]

    assigned_rows.sort(key=lambda r: (r.fullname or "").lower())

    course_roster: List[CourseStudent] = []
    for r in all_course_rows:
        f_name, s_name = split_fullname(r.fullname)
        course_roster.append(
            CourseStudent(
                userid=r.userid,
                fullname=r.fullname,
                firstname=f_name,
                surname=s_name,
                participant_id=r.participant_id,
                moodle_folder=r.moodle_folder,
                matriculation_number=r.matriculation_number,
            )
        )

    print(
        f"Found {len(assigned_rows)} student(s) assigned to you. Downloading PDF submissions..."
    )
    files_by_userid = client.download_submission_files_and_folders(cmid, assigned_rows)

    to_mark_dir.mkdir(parents=True, exist_ok=True)
    marked_dir.mkdir(parents=True, exist_ok=True)

    used_filenames: set[str] = set()
    records: List[StudentSubmissionRecord] = []
    downloaded_count = 0
    partners_matched = 0

    for row in assigned_rows:
        firstname, surname = split_fullname(row.fullname)
        pdf_name = format_student_pdf_name(
            firstname=firstname,
            surname=surname,
            sheet_number=sheet_num,
            used_filenames=used_filenames,
            userid=row.userid,
        )
        student_files = files_by_userid.get(row.userid, [])
        original_names = [fname for fname, _ in student_files]

        rec = StudentSubmissionRecord(
            userid=row.userid,
            fullname=row.fullname,
            firstname=firstname,
            surname=surname,
            filename=pdf_name,
            sheet_number=sheet_num,
            participant_id=row.participant_id,
            moodle_folder=row.moodle_folder,
            original_filenames=original_names,
            current_grade=row.current_grade,
        )

        out_path: Optional[Path] = None
        if student_files:
            merged_bytes = merge_pdf_bytes_list(student_files)
            out_path = to_mark_dir / pdf_name
            out_path.write_bytes(merged_bytes)
            downloaded_count += 1

        partner = detect_partner_for_record(rec, out_path, course_roster)
        if partner:
            partners_matched += 1

        records.append(rec)
        partner_info = f" (Partner: {rec.partner_fullname})" if rec.partner_fullname else ""
        if out_path:
            print(f"  [OK] {row.fullname}{partner_info} -> {out_path.name}")
        else:
            print(f"  [NO SUBMISSION] {row.fullname}{partner_info} (no file uploaded yet)")

    save_manifest(
        manifest_path=manifest_path,
        course_id=course_id,
        cmid=cmid,
        sheet_number=sheet_num,
        records=records,
        course_roster=course_roster,
    )
    sync_grades_csv(grades_csv_path, records)

    print("\nDownload summary:")
    print(f"  Assigned students : {len(records)}")
    print(f"  PDFs saved        : {downloaded_count}")
    print(f"  Partners matched  : {partners_matched}")
    print(f"  To-mark folder    : {to_mark_dir}")
    print(f"  Marked export dir : {marked_dir}")
    print(f"  Grades CSV        : {grades_csv_path}")
    print(f"  Manifest          : {manifest_path}")
    return 0


def cmd_detect_partners(args: argparse.Namespace) -> int:
    """Scan local PDFs with text extraction and local OCR to match group partners from the course roster."""
    settings = load_user_settings()
    sheet_num: int = args.sheet
    workdir_str = args.workdir or str(settings.get("workdir") or "./sheets")
    workdir = resolve_storage_path(workdir_str)
    _, to_mark_dir, marked_dir, manifest_path, grades_csv_path = sheet_paths(
        workdir, sheet_num
    )

    if not manifest_path.exists():
        print(f"Manifest not found at {manifest_path}. Run 'download --sheet {sheet_num}' first.")
        return 1

    course_id, cmid, _, students_by_filename, course_roster = load_manifest_with_roster(
        manifest_path
    )
    matched_count = 0
    for fname, rec in students_by_filename.items():
        pdf_path = to_mark_dir / fname
        if not pdf_path.exists() and (marked_dir / fname).exists():
            pdf_path = marked_dir / fname
        partner = detect_partner_for_record(
            rec, pdf_path if pdf_path.exists() else None, course_roster
        )
        if partner:
            matched_count += 1
            print(f"  [MATCH] {rec.fullname} -> Partner: {partner.fullname} ({rec.partner_match_source})")
        else:
            print(f"  [NONE]  {rec.fullname} -> No second student detected")

    records_list = list(students_by_filename.values())
    save_manifest(
        manifest_path,
        course_id,
        cmid,
        sheet_num,
        records_list,
        course_roster=course_roster,
    )
    sync_grades_csv(grades_csv_path, records_list)
    print(f"\nMatched {matched_count} group partner(s) out of {len(records_list)} submission(s).")
    return 0


def cmd_upload(args: argparse.Namespace) -> int:
    """Match marked PDFs to their specific Moodle submissions (and group partners) and upload files and points."""
    settings = load_user_settings()
    sheet_num: int = args.sheet
    workdir_str = args.workdir or str(settings.get("workdir") or "./sheets")
    workdir = resolve_storage_path(workdir_str)
    _, to_mark_dir, marked_dir, manifest_path, grades_csv_path = sheet_paths(
        workdir, sheet_num
    )

    if not manifest_path.exists():
        print(
            f"Manifest not found at {manifest_path}. Run 'download --sheet {sheet_num}' first."
        )
        return 1

    course_id, cmid, _, students_by_filename, course_roster = load_manifest_with_roster(
        manifest_path
    )
    csv_points = read_grades_csv(grades_csv_path)

    source_dir = Path(args.marked_dir).expanduser().resolve() if args.marked_dir else marked_dir
    primary_items = match_marked_pdfs(
        marked_dir=source_dir,
        students_by_filename=students_by_filename,
        csv_points=csv_points,
    )

    # If marked/ is empty and user marked files directly inside to_mark/, check to_mark/
    if not primary_items and not args.marked_dir and to_mark_dir.exists():
        print(f"No PDFs found in {marked_dir}, checking {to_mark_dir}...")
        primary_items = match_marked_pdfs(
            marked_dir=to_mark_dir,
            students_by_filename=students_by_filename,
            csv_points=csv_points,
        )

    if not primary_items:
        print(
            f"No matching marked PDFs found in {source_dir}. "
            f"Export your annotated PDFs from Notability to {marked_dir} first."
        )
        return 1

    # Prompt for missing points interactively unless disabled
    if not args.no_prompt_points and sys.stdin.isatty():
        updated_csv = False
        for item in primary_items:
            if item.points is None or str(item.points).strip() == "":
                prompt_str = (
                    f"Points for {item.record.fullname} ({item.pdf_path.name}) "
                    f"[press Enter to skip]: "
                )
                entered = input(prompt_str).strip()
                if entered:
                    item.points = entered
                    item.record.current_grade = entered
                    updated_csv = True
            else:
                item.record.current_grade = str(item.points)
        if updated_csv:
            sync_grades_csv(grades_csv_path, list(students_by_filename.values()), overwrite=True)

    # Expand items for grading so both submitting student and partner receive the points and grader
    all_graded_items = expand_marked_items_with_partners(primary_items, course_roster)

    print("\nPrepared upload batch (annotated PDF to submitting student, points to both):")
    for item in primary_items:
        pts_display = item.points if item.points is not None else "(no grade change)"
        partner_label = (
            f" + Partner: {item.record.partner_fullname}"
            if item.record.partner_fullname
            else ""
        )
        print(
            f"  - {item.record.fullname}{partner_label} (userid={item.record.userid}): "
            f"PDF='{item.pdf_path.name}', Points={pts_display}"
        )

    all_have_moodle_folder = all(
        bool(it.record.moodle_folder or it.record.participant_id) for it in primary_items
    )

    if args.dry_run:
        if all_have_moodle_folder:
            zip_bytes = build_moodle_feedback_zip(primary_items)
            print(
                f"\n[DRY RUN] Built valid Moodle feedback ZIP ({len(zip_bytes)} bytes) "
                f"for {len(primary_items)} submitting student(s) ({len(all_graded_items)} graded total). No network requests sent."
            )
        else:
            print(
                f"\n[DRY RUN] Verified direct per-student upload mapping "
                f"for {len(primary_items)} submitting student(s) ({len(all_graded_items)} graded total). No network requests sent."
            )
        return 0

    username, password = prompt_credentials_in_memory(
        args.username or str(settings.get("username") or "")
    )
    client = LMUMoodleClient()
    print("Authenticating with lmuidp.lrz.de and moodle.lmu.de...")
    client.login(username=username, password=password)
    del password

    # 1. Resolve numeric Moodle userids for all submitting students and partners
    for g_it in all_graded_items:
        if not str(g_it.record.userid).isdigit():
            resolved_row = client.resolve_student_userid_on_moodle(
                cmid,
                g_it.record.fullname,
                participant_id=g_it.record.participant_id,
                course_id=course_id,
            )
            if resolved_row and str(resolved_row.userid).isdigit():
                g_it.record.userid = str(resolved_row.userid)
                if resolved_row.participant_id and not g_it.record.participant_id:
                    g_it.record.participant_id = resolved_row.participant_id
                if resolved_row.moodle_folder and not g_it.record.moodle_folder:
                    g_it.record.moodle_folder = resolved_row.moodle_folder

    # Filter out any students whose submissions have already been graded by someone else
    all_graded_items, skipped_items = client.filter_items_graded_by_others(
        cmid, all_graded_items
    )
    skipped_ids = {id(it) for it, _ in skipped_items}
    primary_items = [it for it in primary_items if id(it) not in skipped_ids]

    if skipped_items:
        print(f"\n[Notice] Ignored {len(skipped_items)} student(s) already graded by someone else:")
        for it, reason in skipped_items:
            print(f"  - {it.record.fullname}: {reason}")
        print()

    if not all_graded_items:
        print("No submissions left to upload after filtering.")
        return 0

    # 2. Allocate logged-in tutor as Bewerter (allocatedmarker) for BOTH students first
    client.allocate_marker_to_users(
        cmid, [it.record.userid for it in all_graded_items]
    )

    # 3. Check if participant IDs can be resolved from worksheet if not already present
    if not all_have_moodle_folder:
        part_map = client.fetch_participant_id_map(cmid)
        if part_map:
            for it in all_graded_items:
                if not it.record.participant_id:
                    p_id = part_map.get(it.record.fullname.lower())
                    if p_id:
                        it.record.participant_id = p_id
                        it.record.moodle_folder = f"{it.record.fullname}_{p_id}_assignsubmission_file_"
            all_have_moodle_folder = all(
                bool(it.record.moodle_folder or it.record.participant_id) for it in primary_items
            )

    # Upload annotated feedback PDFs to submitting students and partners
    uploaded_pdfs = 0
    if all_have_moodle_folder:
        try:
            zip_items = [
                it for it in all_graded_items if it.record.moodle_folder or it.record.participant_id
            ]
            zip_bytes = build_moodle_feedback_zip(zip_items)
            print("Uploading feedback PDFs via Moodle bulk feedback ZIP...")
            if client.upload_bulk_feedback_zip(cmid=cmid, zip_bytes=zip_bytes):
                uploaded_pdfs = len(primary_items)
        except Exception as exc:
            print(
                f"Bulk ZIP upload fell back to per-student upload ({exc}). Uploading individually..."
            )

    if uploaded_pdfs < len(primary_items):
        batch_count, remaining = client.upload_batch_feedback_pdfs(cmid=cmid, items=primary_items)
        uploaded_pdfs += batch_count
        for item in remaining:
            print(f"Uploading annotated PDF for {item.record.fullname}...")
            try:
                if client.upload_single_student_feedback_pdf(cmid=cmid, item=item):
                    uploaded_pdfs += 1
            except Exception as single_exc:
                print(
                    f"  [Notice] Feedback PDF upload skipped for {item.record.fullname}: {single_exc}"
                )

    for p_item in [
        it for it in all_graded_items if it.record.partner_match_source == "partner_expansion"
    ]:
        print(f"Uploading annotated PDF for partner {p_item.record.fullname}...")
        try:
            if client.upload_single_student_feedback_pdf(cmid=cmid, item=p_item):
                uploaded_pdfs += 1
        except Exception as p_exc:
            print(
                f"  [Notice] Partner PDF upload skipped for {p_item.record.fullname}: {p_exc}"
            )

    # 4. Enter numerical points and set logged-in grader for BOTH submitting student and partner
    graded_count = client.submit_grades_to_grading_table(cmid=cmid, items=all_graded_items)
    client.allocate_marker_to_users(
        cmid, [it.record.userid for it in all_graded_items]
    )

    manifest_updated = False
    for g_it in all_graded_items:
        if (
            g_it.record.partner_match_source == "partner_expansion"
            and str(g_it.record.userid).isdigit()
        ):
            orig_rec = students_by_filename.get(g_it.record.filename)
            if orig_rec and orig_rec.partner_userid != str(g_it.record.userid):
                orig_rec.partner_userid = str(g_it.record.userid)
                manifest_updated = True
            for st in course_roster:
                if st.fullname.lower() == g_it.record.fullname.lower() and st.userid != str(g_it.record.userid):
                    st.userid = str(g_it.record.userid)
                    manifest_updated = True
    if manifest_updated:
        records_list = list(students_by_filename.values())
        save_manifest(
            manifest_path,
            course_id,
            cmid,
            sheet_num,
            records_list,
            course_roster=course_roster,
        )
        sync_grades_csv(grades_csv_path, records_list, overwrite=True)

    print("\nUpload complete (kept unreleased in Moodle Marking Workflow):")
    print(f"  Course ID              : {course_id}")
    print(f"  Assignment cmid        : {cmid}")
    print(f"  Annotated PDFs uploaded: {uploaded_pdfs}/{len(all_graded_items)}")
    print(f"  Grades entered         : {graded_count}/{len(all_graded_items)}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Show local grading status for a given exercise sheet."""
    settings = load_user_settings()
    sheet_num: int = args.sheet
    workdir_str = args.workdir or str(settings.get("workdir") or "./sheets")
    workdir = resolve_storage_path(workdir_str)
    _, to_mark_dir, marked_dir, manifest_path, grades_csv_path = sheet_paths(
        workdir, sheet_num
    )

    if not manifest_path.exists():
        print(f"No manifest found for sheet {sheet_num} at {manifest_path}.")
        return 1

    course_id, cmid, _, students_by_filename = load_manifest(manifest_path)
    csv_points = read_grades_csv(grades_csv_path)
    matched = match_marked_pdfs(marked_dir, students_by_filename, csv_points)
    matched_by_fname = {m.record.filename: m for m in matched}

    print(f"Status for Übungsblatt {sheet_num} (course={course_id}, cmid={cmid}):")
    for fname, rec in students_by_filename.items():
        in_to_mark = (to_mark_dir / fname).exists()
        marked_item = matched_by_fname.get(fname)
        pts = (
            marked_item.points
            if marked_item and marked_item.points
            else csv_points.get(fname, rec.current_grade or "-")
        )
        state = "MARKED" if marked_item else ("DOWNLOADED" if in_to_mark else "NO_PDF")
        partner_str = f" + Partner: {rec.partner_fullname}" if rec.partner_fullname else ""
        print(f"  [{state:10s}] {fname:28s} | {rec.fullname}{partner_str} | Points: {pts}")
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    """Launch the local loopback graphical interface."""
    from gui import run_gui_server

    run_gui_server(port=args.port, open_browser=not args.no_browser)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Automate LMU Moodle submission downloads, partner OCR matching, and Notability PDF/grade uploads."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # download subcommand
    dl = subparsers.add_parser(
        "download",
        help="Download assigned student PDFs as [FirstInitial][Lastname]_sheet[N].pdf",
    )
    dl.add_argument(
        "-s", "--sheet", type=int, required=True, help="Exercise sheet number (e.g. 1)"
    )
    dl.add_argument(
        "--course-id",
        type=int,
        default=DEFAULT_COURSE_ID,
        help=f"Moodle course ID (default: {DEFAULT_COURSE_ID})",
    )
    dl.add_argument(
        "--cmid",
        type=int,
        default=None,
        help="Optional explicit Moodle assignment cmid override",
    )
    dl.add_argument(
        "--marker",
        type=str,
        default=None,
        help="Optional override for your name in the 'Bewerter' column",
    )
    dl.add_argument(
        "-u", "--username", type=str, default=None, help="LMU username"
    )
    dl.add_argument(
        "--workdir",
        type=str,
        default=None,
        help="Storage directory (e.g. ~icloud/Grading or ./sheets, saved in settings.json)",
    )
    dl.set_defaults(func=cmd_download)

    # detect-partners subcommand
    dp = subparsers.add_parser(
        "detect-partners",
        help="Scan downloaded PDFs via text extraction and local OCR to match the 2nd student in Ana 1",
    )
    dp.add_argument(
        "-s", "--sheet", type=int, required=True, help="Exercise sheet number (e.g. 1)"
    )
    dp.add_argument(
        "--workdir",
        type=str,
        default=None,
        help="Storage directory (e.g. ~icloud/Grading or ./sheets)",
    )
    dp.set_defaults(func=cmd_detect_partners)

    # upload subcommand
    up = subparsers.add_parser(
        "upload",
        help="Upload marked Notability PDFs and enter numerical points into Moodle for both group members",
    )
    up.add_argument(
        "-s", "--sheet", type=int, required=True, help="Exercise sheet number (e.g. 1)"
    )
    up.add_argument(
        "--marked-dir",
        type=str,
        default=None,
        help="Folder containing exported Notability PDFs (default: <workdir>/sheet_<n>/marked)",
    )
    up.add_argument(
        "--no-prompt-points",
        action="store_true",
        help="Do not interactively prompt for missing points",
    )
    up.add_argument(
        "--dry-run",
        action="store_true",
        help="Verify PDF mappings and points without sending requests to Moodle",
    )
    up.add_argument(
        "-u", "--username", type=str, default=None, help="LMU username"
    )
    up.add_argument(
        "--workdir",
        type=str,
        default=None,
        help="Storage directory (e.g. ~icloud/Grading or ./sheets)",
    )
    up.set_defaults(func=cmd_upload)

    # status subcommand
    st = subparsers.add_parser(
        "status",
        help="Show local download, marking, partner, and points status for a sheet",
    )
    st.add_argument(
        "-s", "--sheet", type=int, required=True, help="Exercise sheet number (e.g. 1)"
    )
    st.add_argument(
        "--workdir",
        type=str,
        default=None,
        help="Storage directory (e.g. ~icloud/Grading or ./sheets)",
    )
    st.set_defaults(func=cmd_status)

    # gui subcommand
    gui_p = subparsers.add_parser(
        "gui",
        help="Launch the local graphical web UI (opens full-screen in a-Shell internalbrowser)",
    )
    gui_p.add_argument(
        "--port", type=int, default=8765, help="Localhost port (default: 8765)"
    )
    gui_p.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not automatically open the browser window",
    )
    gui_p.set_defaults(func=cmd_gui)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (SecurityError, MoodleAuthError, MoodleScrapingError) as err:
        print(f"Error: {err}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
