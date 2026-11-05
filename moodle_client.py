"""Secure LMU Moodle client for Shibboleth SAML2 login, Bewerter filtering, and grading."""

from __future__ import annotations

import io
import json
import random
import re
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

try:
    import certifi

    DEFAULT_TLS_VERIFY: Union[bool, str] = certifi.where()
except ImportError:
    DEFAULT_TLS_VERIFY = True

from pdf_processor import MarkedSubmissionItem, StudentSubmissionRecord


MOODLE_BASE_URL = "https://moodle.lmu.de"
IDP_BASE_URL = "https://lmuidp.lrz.de"
ALLOWED_AUTH_HOSTS = {"moodle.lmu.de", "lmuidp.lrz.de"}
DEFAULT_COURSE_ID = 48957
KNOWN_SHEET_CMIDS: Dict[Tuple[int, int], int] = {
    (48957, 1): 2672327,
}


class SecurityError(RuntimeError):
    """Raised when an authentication URL violates HTTPS or host pinning rules."""


class MoodleAuthError(RuntimeError):
    """Raised when Shibboleth or Moodle authentication fails."""


class MoodleScrapingError(RuntimeError):
    """Raised when expected Moodle page elements cannot be found."""


@dataclass
class RawSubmissionRow:
    """Parsed row from the Moodle grading table."""

    userid: str
    fullname: str
    allocated_marker: str = ""
    allocated_marker_id: str = ""
    file_links: List[Tuple[str, str]] = field(default_factory=list)
    current_grade: str = ""
    participant_id: Optional[str] = None
    moodle_folder: Optional[str] = None
    matriculation_number: Optional[str] = None
    grader: Optional[str] = None


def assert_https_and_host(url: str, allowed_hosts: set[str]) -> None:
    """Ensure a URL uses HTTPS and points to an explicitly trusted hostname."""
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https":
        raise SecurityError(f"Refusing non-HTTPS URL: {url}")
    hostname = (parsed.hostname or "").lower()
    if hostname not in allowed_hosts:
        raise SecurityError(
            f"Refusing untrusted host '{hostname}' in URL '{url}'. "
            f"Allowed hosts: {sorted(allowed_hosts)}"
        )


def _strip_leading_initials(name: str) -> str:
    cleaned = " ".join((name or "").strip().split())
    tokens = cleaned.split(" ")
    if (
        len(tokens) >= 3
        and len(tokens[0]) == 2
        and tokens[0].isupper()
        and tokens[0].isalpha()
        and tokens[1]
        and tokens[-1]
    ):
        if tokens[0] == (tokens[1][0] + tokens[-1][0]).upper():
            return " ".join(tokens[1:])
    return cleaned


def _normalize_person_tokens(name: str) -> set[str]:
    """Break a person's name into lowercase alphanumeric tokens for order-independent matching."""
    cleaned = re.sub(r"[^\w\s]", " ", _strip_leading_initials(name).lower())
    return {tok for tok in cleaned.split() if len(tok) > 1}


def names_match(candidate: str, target: str) -> bool:
    """Return True if candidate marker name matches the logged-in user's name."""
    c_clean = _strip_leading_initials(candidate).lower()
    t_clean = _strip_leading_initials(target).lower()
    if not c_clean or not t_clean:
        return False
    if c_clean == t_clean or t_clean in c_clean or c_clean in t_clean:
        return True
    c_tokens = _normalize_person_tokens(candidate)
    t_tokens = _normalize_person_tokens(target)
    return bool(c_tokens and t_tokens and (c_tokens == t_tokens or t_tokens.issubset(c_tokens)))


class LMUMoodleClient:
    """Client for interacting with moodle.lmu.de via LMU Shibboleth SSO."""

    def __init__(
        self,
        base_url: str = MOODLE_BASE_URL,
        idp_url: str = IDP_BASE_URL,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.idp_url = idp_url.rstrip("/")
        self.verify_tls: Union[bool, str] = DEFAULT_TLS_VERIFY
        self.session = session or requests.Session()
        self.session.verify = self.verify_tls
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"
                )
            }
        )
        self.sesskey: Optional[str] = None
        self.userid: Optional[str] = None
        self.fullname: Optional[str] = None
        self.progress_callback = None
        self.upload_repo_id: Optional[str] = None
        self.last_all_rows: List[RawSubmissionRow] = []

    def _report_progress(self, message: str) -> None:
        print(f"[Moodle] {message}", flush=True)
        if callable(self.progress_callback):
            try:
                self.progress_callback(message)
            except Exception:
                pass

    def login(self, username: str, password: str) -> None:
        """Authenticate against lmuidp.lrz.de and establish a Moodle session in memory."""
        if not username or not password:
            raise MoodleAuthError("Username and password must not be empty.")

        shib_init_url = (
            f"{self.base_url}/Shibboleth.sso/Login"
            f"?entityID=https%3A%2F%2Flmuidp.lrz.de%2Fidp%2Fshibboleth"
            f"&target=https%3A%2F%2Fmoodle.lmu.de%2Fauth%2Fshibboleth%2Findex.php"
        )
        assert_https_and_host(shib_init_url, ALLOWED_AUTH_HOSTS)

        resp1 = self.session.get(shib_init_url, verify=self.verify_tls, timeout=30)
        resp1.raise_for_status()
        assert_https_and_host(resp1.url, ALLOWED_AUTH_HOSTS)

        # Step 1: Handle Shibboleth IdP LocalStorage check page (execution=e1s1) if present
        soup1 = BeautifulSoup(resp1.text, "html.parser")
        form1 = soup1.find("form")
        if form1 and soup1.find("input", {"name": "shib_idp_ls_supported"}):
            action1 = urljoin(resp1.url, form1.get("action", ""))
            assert_https_and_host(action1, {"lmuidp.lrz.de"})
            payload1 = self._extract_form_inputs(form1)
            payload1["shib_idp_ls_supported"] = "false"
            payload1["_eventId_proceed"] = ""
            resp2 = self.session.post(action1, data=payload1, verify=self.verify_tls, timeout=30)
            resp2.raise_for_status()
        else:
            resp2 = resp1

        # Step 2: Submit credentials ONLY to https://lmuidp.lrz.de (execution=e1s2)
        assert_https_and_host(resp2.url, {"lmuidp.lrz.de"})
        soup2 = BeautifulSoup(resp2.text, "html.parser")
        login_form = soup2.find("form")
        if not login_form or not soup2.find("input", {"name": "j_username"}):
            raise MoodleAuthError(
                "Could not find the LMU Shibboleth login form on lmuidp.lrz.de."
            )

        login_action = urljoin(resp2.url, login_form.get("action", ""))
        assert_https_and_host(login_action, {"lmuidp.lrz.de"})

        login_payload = self._extract_form_inputs(login_form)
        login_payload["j_username"] = username
        login_payload["j_password"] = password
        login_payload["_eventId_proceed"] = ""

        # Explicitly clear local password variable reference right after building request
        del password

        resp3 = self.session.post(
            login_action, data=login_payload, verify=self.verify_tls, timeout=30
        )
        login_payload.clear()
        resp3.raise_for_status()
        assert_https_and_host(resp3.url, ALLOWED_AUTH_HOSTS)

        soup3 = BeautifulSoup(resp3.text, "html.parser")

        # Step 3: Check if IdP displayed an attribute-release consent screen before SAMLResponse
        if not soup3.find("input", {"name": "SAMLResponse"}):
            error_box = soup3.find(class_=re.compile(r"form__error|output--error"))
            if error_box and error_box.get_text(strip=True):
                raise MoodleAuthError(
                    f"LMU Shibboleth login failed: {error_box.get_text(' ', strip=True)}"
                )
            consent_form = soup3.find("form")
            if consent_form and soup3.find("input", {"name": re.compile(r"_shib_idp_consent")}):
                consent_action = urljoin(resp3.url, consent_form.get("action", ""))
                assert_https_and_host(consent_action, {"lmuidp.lrz.de"})
                consent_payload = self._extract_form_inputs(consent_form)
                consent_payload["_eventId_proceed"] = ""
                resp3 = self.session.post(
                    consent_action, data=consent_payload, verify=self.verify_tls, timeout=30
                )
                resp3.raise_for_status()
                soup3 = BeautifulSoup(resp3.text, "html.parser")

        # Step 4: Submit SAMLResponse to Moodle Service Provider
        saml_input = soup3.find("input", {"name": "SAMLResponse"})
        if saml_input:
            saml_form = saml_input.find_parent("form")
            if not saml_form:
                raise MoodleAuthError("Found SAMLResponse input without parent form.")
            acs_url = urljoin(resp3.url, saml_form.get("action", ""))
            assert_https_and_host(acs_url, {"moodle.lmu.de"})
            saml_payload = self._extract_form_inputs(saml_form)
            resp_final = self.session.post(
                acs_url, data=saml_payload, verify=self.verify_tls, timeout=30
            )
            resp_final.raise_for_status()
        else:
            resp_final = resp3

        assert_https_and_host(resp_final.url, {"moodle.lmu.de"})
        self._extract_user_context(resp_final.text)
        if not self.sesskey:
            raise MoodleAuthError(
                "Authentication finished, but no Moodle sesskey was found. Please check your LMU credentials."
            )

    @staticmethod
    def _extract_form_inputs(form_tag) -> Dict[str, str]:
        """Extract default input key-value pairs from a BeautifulSoup form element."""
        data: Dict[str, str] = {}
        for inp in form_tag.find_all("input"):
            name = inp.get("name")
            if not name:
                continue
            itype = (inp.get("type") or "text").lower()
            if itype in ("checkbox", "radio"):
                if inp.has_attr("checked"):
                    data[name] = inp.get("value", "1")
            elif itype not in ("submit", "button", "reset", "cancel", "image"):
                data[name] = inp.get("value", "")
        for ta in form_tag.find_all("textarea"):
            name = ta.get("name")
            if not name:
                continue
            data[name] = ta.get_text() or ""
        for sel in form_tag.find_all("select"):
            name = sel.get("name")
            if not name:
                continue
            selected = sel.find("option", selected=True) or sel.find("option")
            if selected and selected.has_attr("value"):
                data[name] = selected["value"]
        # Never include cancel or secondary navigation buttons by default
        for cancel_key in (
            "cancelbutton",
            "cancel",
            "nosaveandnext",
            "nosaveandprevious",
            "saveandshownext",
        ):
            data.pop(cancel_key, None)
        return data

    def _extract_user_context(self, html: str) -> None:
        """Extract Moodle sesskey, logged-in userid, and logged-in full name from page HTML."""
        sess_match = re.search(r'"sesskey"\s*:\s*"([^"]+)"', html)
        if not sess_match:
            sess_match = re.search(r'name="sesskey"\s+value="([^"]+)"', html)
        if sess_match:
            self.sesskey = sess_match.group(1)

        # Authoritative logged-in userId from M.cfg JSON
        uid_match = re.search(r'"userId"\s*:\s*(\d+)', html)
        if uid_match and int(uid_match.group(1)) > 0:
            self.userid = uid_match.group(1)
        elif not self.userid:
            # Check mod_assign_grading_options_form which stores $USER->id in <input name="userid">
            opt_uid = re.search(
                r'_qf__mod_assign_grading_options_form.*?name="userid"\s+value="(\d+)"'
                r'|name="userid"\s+value="(\d+)".*?_qf__mod_assign_grading_options_form',
                html,
                flags=re.DOTALL,
            )
            if opt_uid:
                cand_u = opt_uid.group(1) or opt_uid.group(2)
                if cand_u and int(cand_u) > 0:
                    self.userid = cand_u
            else:
                fallback_uid = re.search(r'data-userid="(\d+)"', html) or re.search(
                    r'/user/profile\.php\?id=(\d+)', html
                )
                if fallback_uid and int(fallback_uid.group(1)) > 0:
                    self.userid = fallback_uid.group(1)

        if not self.fullname:
            soup = BeautifulSoup(html, "html.parser")
            usertext = soup.find(class_="usertext")
            if usertext and usertext.get_text(strip=True):
                self.fullname = usertext.get_text(" ", strip=True)
            else:
                logininfo = soup.find(class_="logininfo")
                if logininfo:
                    profile_link = logininfo.find("a", href=re.compile(r"/user/(?:profile|view)\.php"))
                    if profile_link and profile_link.get_text(strip=True):
                        self.fullname = profile_link.get_text(" ", strip=True)

            # Moodle 4.x fallback: check markerfilter or allocatedmarker option matching self.userid
            if not self.fullname and self.userid:
                opt = soup.find("option", {"value": str(self.userid)})
                if opt and opt.get_text(strip=True):
                    cand = opt.get_text(" ", strip=True)
                    if not cand.isdigit():
                        self.fullname = cand

    def find_assignment_cmid(
        self, course_id: int = DEFAULT_COURSE_ID, sheet_number: int = 1
    ) -> int:
        """Locate the Moodle Course Module ID (cmid) for 'Übungsblatt <sheet_number>'."""
        if (course_id, sheet_number) in KNOWN_SHEET_CMIDS:
            cmid = KNOWN_SHEET_CMIDS[(course_id, sheet_number)]
            self._report_progress(f"Using cmid {cmid} for Übungsblatt {sheet_number}.")
            return cmid

        self._report_progress(f"Searching course {course_id} for Übungsblatt {sheet_number}...")
        course_url = f"{self.base_url}/course/view.php?id={course_id}"
        assert_https_and_host(course_url, {"moodle.lmu.de"})
        resp = self.session.get(course_url, verify=self.verify_tls, timeout=30)
        resp.raise_for_status()
        if not self.sesskey:
            self._extract_user_context(resp.text)

        cmid = self.parse_assignment_cmid_from_course_html(resp.text, sheet_number)
        if cmid is not None:
            KNOWN_SHEET_CMIDS[(course_id, sheet_number)] = cmid
            return cmid

        raise MoodleScrapingError(
            f"Could not find 'Übungsblatt {sheet_number}' on course page {course_url}."
        )

    @staticmethod
    def parse_assignment_cmid_from_course_html(
        html: str, sheet_number: int
    ) -> Optional[int]:
        """Parse course HTML to find the cmid for Übungsblatt <sheet_number>."""
        soup = BeautifulSoup(html, "html.parser")
        pattern = re.compile(
            rf"(?:Übungsblatt|Uebungsblatt|Blatt)\s*0*{sheet_number}(?!\d)",
            re.IGNORECASE,
        )

        # First search specifically inside the "Übungsblätter" section if present
        sections = soup.find_all(["li", "div", "section"], class_=re.compile(r"section"))
        for sec in sections:
            header_text = ""
            header = sec.find(
                ["h2", "h3", "h4", "span", "a"],
                class_=re.compile(r"sectionname|section-title"),
            )
            if header:
                header_text = header.get_text(" ", strip=True)
            if re.search(r"Übungsblätter|Uebungsblaetter", header_text, re.IGNORECASE):
                cmid = LMUMoodleClient._search_assign_links_in_container(sec, pattern)
                if cmid is not None:
                    return cmid

        # Fallback: search all assignment links on the course page
        return LMUMoodleClient._search_assign_links_in_container(soup, pattern)

    @staticmethod
    def _search_assign_links_in_container(container, pattern: re.Pattern) -> Optional[int]:
        for link in container.find_all("a", href=re.compile(r"/mod/assign/view\.php\?id=\d+")):
            # Clone or strip accesshide spans when checking text
            instance_span = link.find(class_="instancename")
            target_node = instance_span if instance_span else link
            visible_parts = []
            for child in target_node.descendants:
                if isinstance(child, str):
                    parent_classes = child.parent.get("class", []) if child.parent else []
                    if "accesshide" not in parent_classes:
                        visible_parts.append(child)
            label = " ".join("".join(visible_parts).split()) or link.get_text(" ", strip=True)
            if pattern.search(label):
                match = re.search(r"[?&]id=(\d+)", link["href"])
                if match:
                    return int(match.group(1))
        return None

    def fetch_assigned_submissions(
        self,
        cmid: int,
        marker_query: Optional[str] = None,
    ) -> List[RawSubmissionRow]:
        """Fetch the grading table for cmid and filter rows assigned to the marker in 'Bewerter'."""
        grading_url = (
            f"{self.base_url}/mod/assign/view.php"
            f"?id={cmid}&action=grading&tifirst=&tilast=&perpage=100"
        )
        assert_https_and_host(grading_url, {"moodle.lmu.de"})
        self._report_progress("Fetching Moodle grading table (page 1)...")
        resp = self.session.get(grading_url, verify=self.verify_tls, timeout=30)
        resp.raise_for_status()
        self._extract_user_context(resp.text)

        target_name = (marker_query or self.fullname or "").strip()
        target_userid = (self.userid or "").strip()

        all_rows = self.parse_grading_table_html(resp.text)

        # Extract pagination page numbers without re-parsing the full HTML DOM
        page_nums = sorted(
            {
                int(m.group(1))
                for m in re.finditer(
                    r'href="[^"]*?action=grading[^"]*?[?&]page=(\d+)|href="[^"]*?[?&]page=(\d+)[^"]*?action=grading',
                    resp.text,
                )
                for g in m.groups()
                if g is not None and int(g) > 0
            }
        )
        if not page_nums:
            for nav_match in re.finditer(
                r'class="[^"]*(?:paging|pagination)[^"]*".*?</(?:div|nav|ul)>',
                resp.text,
                re.DOTALL,
            ):
                for pm in re.finditer(r"[?&]page=(\d+)", nav_match.group(0)):
                    pnum = int(pm.group(1))
                    if pnum > 0 and pnum not in page_nums:
                        page_nums.append(pnum)

        for idx, page_num in enumerate(page_nums, start=2):
            self._report_progress(
                f"Fetching Moodle grading table (page {idx}/{len(page_nums) + 1})..."
            )
            p_url = f"{grading_url}&page={page_num}"
            p_resp = self.session.get(p_url, verify=self.verify_tls, timeout=30)
            p_resp.raise_for_status()
            all_rows.extend(self.parse_grading_table_html(p_resp.text))

        # Store deduplicated full course roster for partner matching
        dedup_all: List[RawSubmissionRow] = []
        seen_all = set()
        for r in all_rows:
            if r.userid not in seen_all:
                seen_all.add(r.userid)
                dedup_all.append(r)
        self.last_all_rows = dedup_all

        assigned_rows: List[RawSubmissionRow] = []
        seen_userids = set()
        for row in all_rows:
            if row.userid in seen_userids:
                continue
            if self._is_row_assigned_to_marker(row, target_name, target_userid):
                seen_userids.add(row.userid)
                assigned_rows.append(row)

        # If Moodle already pre-filtered the table for this tutor and hid the Bewerter column
        if all_rows and not assigned_rows:
            distinct_markers = sorted(
                {r.allocated_marker for r in all_rows if r.allocated_marker.strip()}
            )
            if not distinct_markers:
                self._report_progress(
                    f"Grading table has no Bewerter column (pre-filtered by Moodle): using all {len(all_rows)} rows."
                )
                return all_rows
            raise MoodleScrapingError(
                f"Found {len(all_rows)} student rows in the grading table, but none matched "
                f"Bewerter '{target_name or target_userid}'. "
                f"Available Bewerter names in table: {', '.join(distinct_markers[:15])}. "
                f"Enter your exact Bewerter name in the GUI and try again."
            )

        self._report_progress(
            f"Matched {len(assigned_rows)} assigned student(s) out of {len(all_rows)} total rows."
        )
        return assigned_rows

    @staticmethod
    def _is_row_assigned_to_marker(
        row: RawSubmissionRow, target_name: str, target_userid: str
    ) -> bool:
        if target_userid and row.allocated_marker_id and row.allocated_marker_id == target_userid:
            return True
        if target_name and row.allocated_marker and names_match(row.allocated_marker, target_name):
            return True
        return False

    def is_student_graded_by_other(
        self,
        row: Optional[RawSubmissionRow],
        target_name: Optional[str] = None,
        target_userid: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """Check if a student submission was already graded by another tutor.

        Returns a tuple of (is_graded_by_other, reason_description).
        """
        if not row:
            return (False, "")

        grade_str = str(row.current_grade or "").strip()
        if not grade_str or grade_str in ("-", "nicht bewertet", "not graded", "no grade"):
            return (False, "")
        if not re.search(r"\d", grade_str):
            return (False, "")

        candidate_names = [n for n in (target_name, self.fullname) if n and str(n).strip()]
        if not candidate_names:
            try:
                from pdf_processor import load_user_settings

                saved_marker = str(load_user_settings().get("marker") or "").strip()
                if saved_marker:
                    candidate_names.append(saved_marker)
            except Exception:
                pass

        resolved_marker_id = str(
            target_userid or self.userid or self._resolve_logged_in_marker_id() or ""
        ).strip()

        is_my_assignment = False
        for c_name in (candidate_names or [""]):
            if self._is_row_assigned_to_marker(row, c_name, resolved_marker_id):
                is_my_assignment = True
                break

        if is_my_assignment:
            return (False, "")

        if row.grader and any(names_match(row.grader, c_name) for c_name in candidate_names):
            return (False, "")

        other_marker = row.allocated_marker or row.grader or "another tutor"
        reason = (
            f"Submission for {row.fullname} was already graded by {other_marker} "
            f"(existing grade: {grade_str})."
        )
        return (True, reason)

    def filter_items_graded_by_others(
        self,
        cmid: int,
        items: List[MarkedSubmissionItem],
        target_name: Optional[str] = None,
        target_userid: Optional[str] = None,
        live_rows: Optional[List[RawSubmissionRow]] = None,
    ) -> Tuple[List[MarkedSubmissionItem], List[Tuple[MarkedSubmissionItem, str]]]:
        """Inspect submission items and separate those already graded by another tutor.

        Returns a tuple of valid items and skipped items with reasons.
        """
        if not items:
            return ([], [])

        rows = list(live_rows or self.last_all_rows or [])
        if not rows:
            try:
                grading_url = (
                    f"{self.base_url}/mod/assign/view.php"
                    f"?id={cmid}&action=grading&tifirst=&tilast=&perpage=100"
                )
                assert_https_and_host(grading_url, {"moodle.lmu.de"})
                resp = self.session.get(grading_url, verify=self.verify_tls, timeout=20)
                if resp.ok:
                    page_html = self._clear_sticky_grading_filters_if_needed(
                        grading_url, resp.text
                    )
                    rows = self.parse_grading_table_html(page_html)
                    for r in rows:
                        if not any(str(x.userid) == str(r.userid) for x in self.last_all_rows):
                            self.last_all_rows.append(r)
            except Exception:
                rows = list(self.last_all_rows or [])

        by_uid: Dict[str, RawSubmissionRow] = {}
        by_pid: Dict[str, RawSubmissionRow] = {}
        for r in rows:
            if str(r.userid).isdigit():
                by_uid[str(r.userid)] = r
            if r.participant_id:
                by_pid[str(r.participant_id)] = r

        valid_items: List[MarkedSubmissionItem] = []
        skipped_items: List[Tuple[MarkedSubmissionItem, str]] = []

        for item in items:
            uid = str(item.record.userid or "").strip()
            pid = str(item.record.participant_id or "").strip()
            matched_row: Optional[RawSubmissionRow] = None

            if uid.isdigit() and uid in by_uid:
                matched_row = by_uid[uid]
            elif pid and pid in by_pid:
                matched_row = by_pid[pid]
            else:
                for r in rows:
                    if names_match(r.fullname, item.record.fullname):
                        matched_row = r
                        break

            if (not matched_row or not matched_row.current_grade) and uid.isdigit():
                try:
                    grade_url = (
                        f"{self.base_url}/mod/assign/view.php"
                        f"?id={cmid}&action=grade&rownum=0&userid={uid}"
                    )
                    assert_https_and_host(grade_url, {"moodle.lmu.de"})
                    resp_g = self.session.get(grade_url, verify=self.verify_tls, timeout=15)
                    if resp_g.ok:
                        soup_g = BeautifulSoup(resp_g.text, "html.parser")
                        form_g = self._find_grade_or_mform(soup_g)
                        if form_g:
                            g_inp = form_g.find(["input", "select"], {"name": "grade"})
                            curr_g = str(g_inp.get("value") or "").strip() if g_inp else ""
                            m_sel = form_g.find("select", {"name": "allocatedmarker"})
                            cur_marker = ""
                            cur_marker_id = ""
                            if m_sel:
                                opt = m_sel.find("option", selected=True)
                                if opt:
                                    cur_marker = opt.get_text(" ", strip=True)
                                    cur_marker_id = str(opt.get("value") or "").strip()
                            if curr_g:
                                matched_row = RawSubmissionRow(
                                    userid=uid,
                                    fullname=item.record.fullname,
                                    allocated_marker=cur_marker,
                                    allocated_marker_id=cur_marker_id,
                                    current_grade=curr_g,
                                )
                except Exception:
                    pass

            is_graded_by_other, reason = self.is_student_graded_by_other(
                matched_row, target_name, target_userid
            )
            if is_graded_by_other:
                skipped_items.append((item, reason))
            else:
                valid_items.append(item)

        return (valid_items, skipped_items)

    @staticmethod
    def parse_grading_table_html(html: str) -> List[RawSubmissionRow]:
        """Parse all student rows from Moodle's mod_assign grading table HTML."""
        # Slice to the gradingtable <table ...>...</table> block if present to speed up BeautifulSoup on iPad
        table_html = html
        gt_pos = html.find("gradingtable")
        if gt_pos != -1:
            t_start = html.rfind("<table", 0, gt_pos)
            t_end = html.find("</table>", gt_pos)
            if t_start != -1 and t_end != -1:
                table_html = html[t_start : t_end + 8]

        soup = BeautifulSoup(table_html, "html.parser")
        table = soup.find("table", class_=re.compile(r"gradingtable|generaltable|flexible"))
        if not table:
            return []

        # Map column index -> header text / column identifier
        headers: List[str] = []
        thead = table.find("thead")
        header_row = thead.find("tr") if thead else table.find("tr")
        if header_row:
            for th in header_row.find_all(["th", "td"]):
                col_class = " ".join(th.get("class", []))
                col_text = th.get_text(" ", strip=True)
                headers.append(f"{col_class} {col_text}".lower())

        marker_col_idx: Optional[int] = None
        name_col_idx: Optional[int] = None
        grader_col_idx: Optional[int] = None
        for idx, h in enumerate(headers):
            if any(k in h for k in ("allocatedmarker", "bewerter", "allocated marker")):
                marker_col_idx = idx
            if any(k in h for k in ("fullname", "vorname", "nachname", "first name", "surname")):
                if name_col_idx is None:
                    name_col_idx = idx
            if any(k in h for k in ("grader", "bewertet von")):
                grader_col_idx = idx

        rows_out: List[RawSubmissionRow] = []
        tbody = table.find("tbody") or table
        for tr in tbody.find_all("tr"):
            tr_classes = " ".join(tr.get("class", []))
            if "emptyrow" in tr_classes:
                continue

            userid = ""
            uid_match = re.search(r"\buser(\d+)\b", tr_classes)
            if uid_match:
                userid = uid_match.group(1)
            if not userid:
                cb = tr.find("input", {"name": "selectedusers"})
                if cb and cb.get("value"):
                    userid = cb["value"].strip()
            if not userid:
                grade_link = tr.find("a", href=re.compile(r"[?&]userid=(\d+)"))
                if grade_link:
                    m = re.search(r"[?&]userid=(\d+)", grade_link["href"])
                    if m:
                        userid = m.group(1)
            if not userid:
                continue

            cells = tr.find_all(["td", "th"])

            # Extract student full name (ignoring avatar <span class="userinitials"> links in col_picture)
            fullname = ""
            for user_link in tr.find_all(
                "a", href=re.compile(r"/user/(?:view|profile)\.php\?id=\d+")
            ):
                candidate_name = LMUMoodleClient._extract_clean_user_link_name(user_link)
                if candidate_name and len(candidate_name) >= 3:
                    fullname = candidate_name
                    break
            if not fullname and name_col_idx is not None and name_col_idx < len(cells):
                fullname = _strip_leading_initials(cells[name_col_idx].get_text(" ", strip=True))

            # Extract allocated marker ("Bewerter")
            allocated_marker = ""
            allocated_marker_id = ""
            marker_cell = tr.find(
                ["td", "th"], class_=re.compile(r"allocatedmarker", re.IGNORECASE)
            )
            if marker_cell is None and marker_col_idx is not None and marker_col_idx < len(cells):
                marker_cell = cells[marker_col_idx]

            if marker_cell is not None:
                sel = marker_cell.find("select", {"name": re.compile(r"allocatedmarker")})
                if sel:
                    opt = sel.find("option", selected=True)
                    if opt:
                        allocated_marker = opt.get_text(" ", strip=True)
                        allocated_marker_id = (opt.get("value") or "").strip()
                else:
                    allocated_marker = marker_cell.get_text(" ", strip=True)

            # Extract submitted PDF/file links
            file_links: List[Tuple[str, str]] = []
            for a_tag in tr.find_all(
                "a", href=re.compile(r"/pluginfile\.php/.+/assignsubmission_file/")
            ):
                href = a_tag["href"]
                fname = a_tag.get_text(" ", strip=True)
                if not fname:
                    fname = urlparse(href).path.rsplit("/", 1)[-1]
                file_links.append((fname, href))

            # Extract current grade if present
            current_grade = ""
            qg_input = tr.find("input", {"name": f"quickgrade_{userid}"})
            if qg_input and qg_input.get("value") is not None:
                current_grade = qg_input["value"].strip()
            else:
                grade_cell = tr.find(["td", "th"], class_=re.compile(r"\bgrade\b"))
                if grade_cell:
                    m_grade = re.search(
                        r"(\d+(?:[.,]\d+)?)", grade_cell.get_text(" ", strip=True)
                    )
                    if m_grade:
                        current_grade = m_grade.group(1)

            # Extract participant ID if shown in row
            participant_id = None
            moodle_folder = None
            row_text = tr.get_text(" ", strip=True)
            part_match = re.search(
                r"(?:Teilnehmer/in|Participant)\s+(\d+)", row_text, re.IGNORECASE
            )
            if part_match:
                participant_id = part_match.group(1)
                if fullname:
                    moodle_folder = f"{fullname}_{participant_id}_assignsubmission_file_"

            # Extract 8-digit matriculation number (Matrikelnummer / idnumber) if present in row
            matriculation_number = None
            idnum_cell = tr.find(
                ["td", "th"], class_=re.compile(r"idnumber", re.IGNORECASE)
            )
            if idnum_cell:
                m_matr = re.search(r"\b(\d{8})\b", idnum_cell.get_text(" ", strip=True))
                if m_matr:
                    matriculation_number = m_matr.group(1)
            if not matriculation_number:
                for m_matr in re.finditer(r"\b(\d{8})\b", row_text):
                    cand_num = m_matr.group(1)
                    if cand_num not in (userid, participant_id or "", allocated_marker_id):
                        matriculation_number = cand_num
                        break

            # Extract grader (user who entered the grade) if present
            grader = None
            grader_cell = tr.find(["td", "th"], class_=re.compile(r"\bgrader\b", re.IGNORECASE))
            if grader_cell is None and grader_col_idx is not None and grader_col_idx < len(cells):
                grader_cell = cells[grader_col_idx]
            if grader_cell is not None:
                g_text = grader_cell.get_text(" ", strip=True)
                if g_text and g_text != "-":
                    grader = _strip_leading_initials(g_text)

            rows_out.append(
                RawSubmissionRow(
                    userid=userid,
                    fullname=fullname or f"User_{userid}",
                    allocated_marker=allocated_marker,
                    allocated_marker_id=allocated_marker_id,
                    file_links=file_links,
                    current_grade=current_grade,
                    participant_id=participant_id,
                    moodle_folder=moodle_folder,
                    matriculation_number=matriculation_number,
                    grader=grader,
                )
            )

        return rows_out

    def download_submission_files_and_folders(
        self,
        cmid: int,
        assigned_rows: List[RawSubmissionRow],
        use_downloadall_fallback: bool = False,
    ) -> Dict[str, List[Tuple[str, bytes]]]:
        """Download PDF files for assigned students directly via their pluginfile.php submission links."""
        files_by_userid: Dict[str, List[Tuple[str, bytes]]] = {}

        rows_with_files = [r for r in assigned_rows if r.file_links]
        for idx, row in enumerate(rows_with_files, start=1):
            pdf_links = [
                (fname, url)
                for fname, url in row.file_links
                if fname.lower().endswith(".pdf") or ".pdf" in url.lower()
            ]
            if not pdf_links and row.file_links:
                pdf_links = row.file_links

            if pdf_links:
                self._report_progress(
                    f"Downloading PDF {idx}/{len(rows_with_files)}: {row.fullname}..."
                )
                student_payloads: List[Tuple[str, bytes]] = []
                for fname, url in pdf_links:
                    full_url = urljoin(self.base_url, url)
                    assert_https_and_host(full_url, {"moodle.lmu.de"})
                    r = self.session.get(full_url, verify=self.verify_tls, timeout=60)
                    r.raise_for_status()
                    student_payloads.append((fname, r.content))
                files_by_userid[row.userid] = student_payloads

        # Do NOT download the entire course's action=downloadall ZIP unless explicitly requested
        if use_downloadall_fallback and assigned_rows and not files_by_userid:
            self._enrich_from_downloadall_zip(cmid, assigned_rows, files_by_userid)

        return files_by_userid

    def _enrich_from_downloadall_zip(
        self,
        cmid: int,
        assigned_rows: List[RawSubmissionRow],
        files_by_userid: Dict[str, List[Tuple[str, bytes]]],
    ) -> None:
        """Inspect Moodle's downloadall ZIP to extract exact moodle_folder names and any missing PDFs."""
        zip_url = f"{self.base_url}/mod/assign/view.php?id={cmid}&action=downloadall"
        assert_https_and_host(zip_url, {"moodle.lmu.de"})
        resp = self.session.get(zip_url, verify=self.verify_tls, timeout=120)
        if resp.status_code != 200 or not resp.content.startswith(b"PK"):
            return

        with zipfile.ZipFile(io.BytesIO(resp.content), "r") as zf:
            for member in zf.namelist():
                if member.endswith("/"):
                    continue
                parts = member.split("/", 1)
                folder_prefix = parts[0]
                inner_name = parts[1] if len(parts) > 1 else parts[0]

                m = re.match(
                    r"^(.+?)_(\d+)_assignsubmission_file_?(.*)$", folder_prefix
                )
                if not m:
                    continue
                student_name_in_zip = m.group(1).strip()
                participant_id = m.group(2).strip()
                canonical_folder = f"{student_name_in_zip}_{participant_id}_assignsubmission_file_"

                for row in assigned_rows:
                    if names_match(student_name_in_zip, row.fullname):
                        row.participant_id = participant_id
                        row.moodle_folder = canonical_folder
                        if row.userid not in files_by_userid and inner_name.lower().endswith(".pdf"):
                            files_by_userid.setdefault(row.userid, []).append(
                                (inner_name, zf.read(member))
                            )

    def upload_draft_file(
        self,
        filename: str,
        file_bytes: bytes,
        itemid: str,
        ctx_id: str,
        repo_id: Optional[str] = None,
        mime_type: str = "application/pdf",
    ) -> Dict:
        """Upload a file into Moodle's draft file area via repository_ajax.php?action=upload.

        Automatically recovers if a repository ID belongs to a non-upload plugin (such as repository_user).
        """
        if not self.sesskey:
            raise MoodleAuthError("Not logged in (missing sesskey).")

        upload_url = f"{self.base_url}/repository/repository_ajax.php?action=upload"
        assert_https_and_host(upload_url, {"moodle.lmu.de"})

        active_repo = str(repo_id or self.upload_repo_id or "4")
        candidate_repos = [active_repo] + [
            r for r in ["4", "3", "1", "2", "6", "7", "8"] if r != active_repo
        ]

        last_error = ""
        for cur_repo in candidate_repos:
            files = {
                "repo_upload_file": (filename, io.BytesIO(file_bytes), mime_type),
            }
            data = {
                "title": filename,
                "author": self.fullname or "",
                "license": "allrightsreserved",
                "repo_id": str(cur_repo),
                "p": "",
                "page": "",
                "env": "filemanager",
                "sesskey": self.sesskey,
                "client_id": f"client_{random.randint(100000, 999999)}",
                "itemid": str(itemid),
                "maxbytes": "-1",
                "areamaxbytes": "-1",
                "ctx_id": str(ctx_id),
                "savepath": "/",
                "overwrite": "1",
            }
            resp = self.session.post(
                upload_url, data=data, files=files, verify=self.verify_tls, timeout=60
            )
            resp.raise_for_status()
            result = resp.json()
            if isinstance(result, dict) and result.get("error"):
                err_msg = str(result["error"])
                last_error = err_msg
                if any(
                    k in err_msg.lower()
                    for k in (
                        "undefined method repository_",
                        "call to undefined method",
                        "repository_user",
                        "invalid repository",
                    )
                ):
                    continue
                raise MoodleScrapingError(f"Moodle draft upload error: {err_msg}")

            self.upload_repo_id = str(cur_repo)
            return result

        raise MoodleScrapingError(
            f"Moodle draft upload failed across all repository candidates: {last_error}"
        )

    @staticmethod
    def _find_upload_repo_id(html_text: str) -> Optional[str]:
        """Extract the repository instance id for 'upload' from Moodle HTML / JavaScript."""
        import html as html_lib

        text = html_lib.unescape(html_text).replace(r"\"", '"')
        for block in re.finditer(
            r"\{[^{}]*?(?:\"type\"|type)\s*:\s*\"upload\"[^{}]*?\}",
            text,
            re.IGNORECASE,
        ):
            m = re.search(r"(?:\"id\"|id)\s*:\s*\"?(\d+)", block.group(0), re.IGNORECASE)
            if m:
                return m.group(1)

        m = re.search(
            r"(?:\"id\"|id)\s*:\s*\"?(\d+)\"?[^}]*?(?:\"type\"|type)\s*:\s*\"upload\"",
            text,
            re.IGNORECASE,
        )
        if m:
            return m.group(1)

        m = re.search(
            r"(?:\"type\"|type)\s*:\s*\"upload\"[^}]*?(?:\"id\"|id)\s*:\s*\"?(\d+)",
            text,
            re.IGNORECASE,
        )
        if m:
            return m.group(1)

        return None

    def _extract_filemanager_meta(self, html: str, field_name_pattern: str) -> Tuple[str, str, str]:
        """Extract (itemid, ctx_id, upload_repo_id) from a Moodle mform page."""
        soup = BeautifulSoup(html, "html.parser")
        itemid = ""
        inp = soup.find("input", {"name": re.compile(field_name_pattern)})
        if inp and inp.get("value"):
            itemid = inp["value"].strip()
        if not itemid:
            m_item = re.search(r'"itemid"\s*:\s*(\d+)', html)
            if m_item:
                itemid = m_item.group(1)

        ctx_id = ""
        m_ctx = re.search(
            r'"contextid"\s*:\s*(\d+)|"ctx_id"\s*:\s*(\d+)|(?:contextid|ctx_id)["\']?\s*[:=]\s*["\']?(\d+)',
            html,
        )
        if m_ctx:
            ctx_id = m_ctx.group(1) or m_ctx.group(2) or m_ctx.group(3)
        if not ctx_id:
            ctx_inp = soup.find("input", {"name": re.compile(r"ctx_id|contextid")})
            if ctx_inp and ctx_inp.get("value"):
                ctx_id = ctx_inp["value"].strip()

        found_repo = self._find_upload_repo_id(html)
        if found_repo:
            self.upload_repo_id = found_repo

        repo_id = self.upload_repo_id or "4"
        return (itemid, ctx_id, repo_id)

    @staticmethod
    def _extract_clean_user_link_name(a_tag: Any) -> str:
        """Extract a user's full name from a Moodle profile link, excluding avatar <span class="userinitials">."""
        parts: List[str] = []
        for child in a_tag.descendants:
            if isinstance(child, str):
                parent = getattr(child, "parent", None)
                skip = False
                while parent is not None and parent != a_tag:
                    p_classes = (
                        " ".join(parent.get("class", [])) if hasattr(parent, "get") else ""
                    )
                    if any(
                        cls_tok in p_classes.lower()
                        for cls_tok in ("userinitials", "accesshide", "sr-only")
                    ):
                        skip = True
                        break
                    parent = getattr(parent, "parent", None)
                if not skip:
                    txt = child.strip()
                    if txt:
                        parts.append(txt)
        return _strip_leading_initials(" ".join(parts))

    @staticmethod
    def _find_grade_or_mform(soup: BeautifulSoup) -> Optional[Any]:
        """Find a Moodle gradeform (class='gradeform') or moodleform (class='mform') on a parsed page."""
        form = soup.find("form", class_=re.compile(r"\b(?:gradeform|mform)\b"))
        if form is not None:
            return form
        form = soup.find("form", {"id": re.compile(r"^mform")})
        if form is not None:
            return form
        for f_tag in soup.find_all("form"):
            if f_tag.find("input", {"name": re.compile(r"^_qf__")}) is not None:
                return f_tag
        return None

    def fetch_grading_worksheet_rows(self, cmid: int) -> List[RawSubmissionRow]:
        """Fetch student rows from Moodle's offline grading worksheet CSV."""
        candidate_urls = [
            (
                f"{self.base_url}/mod/assign/view.php"
                f"?id={cmid}&plugin=offline&pluginsubtype=assignfeedback"
                f"&action=viewpluginpage&pluginaction=downloadgrades"
            ),
            f"{self.base_url}/mod/assign/view.php?id={cmid}&action=downloadgradingworksheet",
        ]
        import csv

        for worksheet_url in candidate_urls:
            assert_https_and_host(worksheet_url, {"moodle.lmu.de"})
            try:
                resp = self.session.get(worksheet_url, verify=self.verify_tls, timeout=20)
                if not resp.ok or "text/html" in resp.headers.get("Content-Type", ""):
                    continue
                rows_out: List[RawSubmissionRow] = []
                text_content = resp.text.lstrip("\ufeff")
                reader = csv.reader(text_content.splitlines())
                header = next(reader, None)
                if not header:
                    continue
                id_idx = None
                name_idx = None
                matr_idx = None
                for idx, col in enumerate(header):
                    clean_col = col.lower().strip()
                    if any(k in clean_col for k in ("identifier", "teilnehmer-id", "teilnehmer/in")):
                        id_idx = idx
                    elif any(
                        k in clean_col
                        for k in ("full name", "vollständiger name", "fullname", "name")
                    ):
                        if name_idx is None:
                            name_idx = idx
                    elif any(
                        k in clean_col
                        for k in ("id number", "id-nummer", "matrikel", "idnumber")
                    ):
                        matr_idx = idx
                if id_idx is None or name_idx is None:
                    continue
                for row in reader:
                    if len(row) > max(id_idx, name_idx):
                        raw_id = row[id_idx].strip()
                        m = re.search(r"(\d+)", raw_id)
                        part_id = m.group(1) if m else raw_id
                        raw_name = _strip_leading_initials(" ".join(row[name_idx].strip().split()))
                        matr_num = None
                        if matr_idx is not None and len(row) > matr_idx:
                            m8 = re.search(r"\b(\d{8})\b", row[matr_idx].strip())
                            if m8:
                                matr_num = m8.group(1)
                        if part_id and raw_name:
                            rows_out.append(
                                RawSubmissionRow(
                                    userid=f"part_{part_id}",
                                    fullname=raw_name,
                                    participant_id=part_id,
                                    moodle_folder=f"{raw_name}_{part_id}_assignsubmission_file_",
                                    matriculation_number=matr_num,
                                )
                            )
                if rows_out:
                    return rows_out
            except Exception as exc:
                print(f"[Client] Notice: Grading worksheet lookup skipped: {exc}", flush=True)
        return []

    def fetch_participant_id_map(self, cmid: int) -> Dict[str, str]:
        """Attempt to fetch participant IDs from Moodle offline grading worksheet CSV.

        Returns a dictionary mapping lowercase student fullname to participant_id.
        """
        mapping: Dict[str, str] = {}
        for row in self.fetch_grading_worksheet_rows(cmid):
            if row.fullname and row.participant_id:
                mapping[row.fullname.lower()] = row.participant_id
        return mapping

    def fetch_course_participants_rows(self, course_id: int) -> List[RawSubmissionRow]:
        """Fetch enrolled course participants from /user/index.php?id=<course_id> if accessible."""
        from pdf_processor import is_excluded_partner_name

        participants_url = f"{self.base_url}/user/index.php?id={course_id}&perpage=5000"
        assert_https_and_host(participants_url, {"moodle.lmu.de"})
        try:
            resp = self.session.get(participants_url, verify=self.verify_tls, timeout=25)
            if not resp.ok:
                return []
            soup = BeautifulSoup(resp.text, "html.parser")
            rows_out: List[RawSubmissionRow] = []
            seen_uids = set()
            for a_tag in soup.find_all(
                "a", href=re.compile(r"/user/(?:view|profile)\.php\?id=(\d+)")
            ):
                m_uid = re.search(r"[?&]id=(\d+)", a_tag["href"])
                if not m_uid:
                    continue
                uid = m_uid.group(1)
                if uid in seen_uids or uid == self.userid:
                    continue
                fullname = self._extract_clean_user_link_name(a_tag)
                if (
                    not fullname
                    or len(fullname) < 3
                    or fullname.isdigit()
                    or is_excluded_partner_name(fullname)
                ):
                    continue
                seen_uids.add(uid)
                matr_num = None
                tr = a_tag.find_parent("tr")
                if tr:
                    tr_text = tr.get_text(" ", strip=True)
                    for m8 in re.finditer(r"\b(\d{8})\b", tr_text):
                        if m8.group(1) != uid:
                            matr_num = m8.group(1)
                            break
                rows_out.append(
                    RawSubmissionRow(
                        userid=uid,
                        fullname=fullname,
                        matriculation_number=matr_num,
                    )
                )
            return rows_out
        except Exception as exc:
            print(f"[Client] Notice: Course participants lookup skipped: {exc}", flush=True)
            return []

    def _clear_sticky_grading_filters_if_needed(
        self, grading_url: str, html: str, ensure_quickgrade: bool = True
    ) -> str:
        """If Moodle's grading options form has a sticky filter, markerfilter, or disabled quickgrading, reset it."""
        soup = BeautifulSoup(html, "html.parser")
        has_quickgrade_inputs = bool(
            soup.find("input", {"name": re.compile(r"^quickgrade_\d+$")})
        )
        options_form = None
        needs_reset = False
        for f_tag in soup.find_all("form"):
            has_opt_control = False
            for sel_name in ("filter", "markerfilter", "workflowfilter"):
                sel = f_tag.find("select", {"name": sel_name})
                if sel is not None:
                    has_opt_control = True
                    opt = sel.find("option", selected=True)
                    val = (opt.get("value") or "").strip() if opt else ""
                    if val and val != "none":
                        needs_reset = True
            perpage_sel = f_tag.find("select", {"name": "perpage"})
            if perpage_sel is not None:
                has_opt_control = True
                opt_pp = perpage_sel.find("option", selected=True)
                val_pp = (opt_pp.get("value") or "").strip() if opt_pp else ""
                if val_pp in ("10", "20", "50"):
                    needs_reset = True
            qg_chk = f_tag.find("input", {"name": re.compile(r"^quickgrad(?:ing|e)$")})
            if qg_chk is not None:
                has_opt_control = True
                if ensure_quickgrade and not has_quickgrade_inputs:
                    needs_reset = True
            if has_opt_control:
                options_form = f_tag
                break

        if options_form and needs_reset:
            opt_data = self._extract_form_inputs(options_form)
            opt_data["filter"] = ""
            opt_data["markerfilter"] = ""
            opt_data["workflowfilter"] = ""
            opt_data["perpage"] = "100"
            opt_data["quickgrading"] = "1"
            opt_data["quickgrade"] = "1"
            if not opt_data.get("action"):
                opt_data["action"] = "saveoptions"
            sub_btn = options_form.find("input", {"type": "submit"}) or options_form.find(
                "button", {"type": "submit"}
            )
            if sub_btn and sub_btn.get("name"):
                opt_data[sub_btn["name"]] = sub_btn.get("value", "Save")
            else:
                opt_data["submitbutton"] = "Save"

            opt_action = urljoin(grading_url, options_form.get("action", grading_url))
            assert_https_and_host(opt_action, {"moodle.lmu.de"})
            resp_reset = self.session.post(
                opt_action, data=opt_data, verify=self.verify_tls, timeout=30
            )
            if resp_reset.ok:
                # Moodle's saveoptions redirect strips tifirst/tilast/page query parameters.
                # Re-GET grading_url if any of those parameters were requested.
                parsed_q = parse_qs(urlparse(grading_url).query)
                has_specific_view = any(
                    parsed_q.get(k) and parsed_q[k][0] != ""
                    for k in ("tifirst", "tilast", "page")
                )
                if has_specific_view:
                    resp_again = self.session.get(
                        grading_url, verify=self.verify_tls, timeout=30
                    )
                    if resp_again.ok:
                        return resp_again.text
                return resp_reset.text
        return html

    def fetch_full_course_roster(
        self, course_id: int, cmid: int
    ) -> List[RawSubmissionRow]:
        """Gather the complete list of students in the course across grading table, worksheet CSV, and participants page."""
        from pdf_processor import is_excluded_partner_name

        merged_by_uid: Dict[str, RawSubmissionRow] = {}
        uid_by_norm_name: Dict[str, str] = {}
        uid_by_pid: Dict[str, str] = {}

        def _norm_key(name: str) -> str:
            return " ".join(_strip_leading_initials(name).lower().replace(",", " ").split())

        def _add_or_merge(row: RawSubmissionRow) -> None:
            clean_fname = _strip_leading_initials(row.fullname)
            if not clean_fname or is_excluded_partner_name(clean_fname):
                return
            row.fullname = clean_fname
            nkey = _norm_key(clean_fname)
            uid = str(row.userid or "")
            pid = str(row.participant_id or "").strip()
            if not pid and uid.startswith("part_") and uid[5:].isdigit():
                pid = uid[5:]
            is_temp_uid = uid.startswith("part_") or not uid.isdigit()

            existing_uid = None
            if not is_temp_uid and uid in merged_by_uid:
                existing_uid = uid
            elif pid and pid in uid_by_pid and uid_by_pid[pid] in merged_by_uid:
                existing_uid = uid_by_pid[pid]
            elif nkey in uid_by_norm_name and uid_by_norm_name[nkey] in merged_by_uid:
                existing_uid = uid_by_norm_name[nkey]

            if existing_uid and existing_uid in merged_by_uid:
                ex = merged_by_uid[existing_uid]
                if not is_temp_uid and (ex.userid.startswith("part_") or not ex.userid.isdigit()):
                    merged_by_uid.pop(existing_uid, None)
                    ex.userid = uid
                    merged_by_uid[uid] = ex
                    uid_by_norm_name[nkey] = uid
                    if pid:
                        uid_by_pid[pid] = uid
                    if ex.participant_id:
                        uid_by_pid[str(ex.participant_id)] = uid
                if pid and not ex.participant_id:
                    ex.participant_id = pid
                    uid_by_pid[pid] = ex.userid
                if row.moodle_folder and not ex.moodle_folder:
                    ex.moodle_folder = row.moodle_folder
                if row.matriculation_number and not ex.matriculation_number:
                    ex.matriculation_number = row.matriculation_number
            else:
                key = uid if uid else f"name_{nkey}"
                merged_by_uid[key] = RawSubmissionRow(
                    userid=uid,
                    fullname=clean_fname,
                    allocated_marker=row.allocated_marker,
                    allocated_marker_id=row.allocated_marker_id,
                    file_links=list(row.file_links),
                    current_grade=row.current_grade,
                    participant_id=pid or row.participant_id,
                    moodle_folder=row.moodle_folder,
                    matriculation_number=row.matriculation_number,
                )
                uid_by_norm_name[nkey] = key
                if pid:
                    uid_by_pid[pid] = key

        def _fetch_unfiltered_grading_pages() -> None:
            try:
                unfiltered_url = (
                    f"{self.base_url}/mod/assign/view.php"
                    f"?id={cmid}&action=grading&tifirst=&tilast=&perpage=100"
                )
                assert_https_and_host(unfiltered_url, {"moodle.lmu.de"})
                resp_u = self.session.get(unfiltered_url, verify=self.verify_tls, timeout=30)
                if resp_u.ok:
                    page_html = self._clear_sticky_grading_filters_if_needed(
                        unfiltered_url, resp_u.text
                    )
                    for r in self.parse_grading_table_html(page_html):
                        _add_or_merge(r)
                    page_nums = sorted(
                        {
                            int(m.group(1) or m.group(2))
                            for m in re.finditer(
                                r'href="[^"]*?action=grading[^"]*?[?&]page=(\d+)|href="[^"]*?[?&]page=(\d+)[^"]*?action=grading',
                                page_html,
                            )
                            if (m.group(1) or m.group(2)) and int(m.group(1) or m.group(2)) > 0
                        }
                    )
                    for pnum in page_nums:
                        p_resp = self.session.get(
                            f"{unfiltered_url}&page={pnum}",
                            verify=self.verify_tls,
                            timeout=30,
                        )
                        if p_resp.ok:
                            for r in self.parse_grading_table_html(p_resp.text):
                                _add_or_merge(r)
            except Exception:
                pass

        # 1. Start with rows already parsed from the grading table
        for r in self.last_all_rows:
            _add_or_merge(r)

        # 2. If last_all_rows was small (e.g. sticky filter was active in Moodle), fetch unfiltered grading pages
        fetched_unfiltered = False
        if len(merged_by_uid) < 40:
            _fetch_unfiltered_grading_pages()
            fetched_unfiltered = True

        # 3. Merge offline grading worksheet CSV (contains assignment participants + participant_ids)
        self._report_progress("Fetching full course student list from grading worksheet...")
        for r in self.fetch_grading_worksheet_rows(cmid):
            _add_or_merge(r)

        # 4. Merge course participants page (/user/index.php) for complete userid coverage
        self._report_progress("Checking course participants roster...")
        for r in self.fetch_course_participants_rows(course_id):
            _add_or_merge(r)

        # 5. If any student still has a temporary "part_" userid and we haven't fetched the unfiltered grading pages yet, do so now
        if not fetched_unfiltered and any(
            not str(r.userid).isdigit() for r in merged_by_uid.values()
        ):
            _fetch_unfiltered_grading_pages()

        roster_list = sorted(merged_by_uid.values(), key=lambda item: item.fullname.lower())
        self.last_all_rows = roster_list
        self._report_progress(f"Loaded {len(roster_list)} student(s) in the Ana 1 course roster.")
        return roster_list

    def resolve_student_userid_on_moodle(
        self,
        cmid: int,
        fullname: str,
        participant_id: Optional[str] = None,
        course_id: int = DEFAULT_COURSE_ID,
    ) -> Optional[RawSubmissionRow]:
        """Resolve a student's numeric Moodle userid via cached rows, grading table initials, or course participants."""
        from pdf_processor import split_fullname

        clean_target_name = _strip_leading_initials(fullname)
        pid_clean = str(participant_id or "").strip()
        if pid_clean.startswith("part_"):
            pid_clean = pid_clean[5:]

        # 1. Check already-cached rows first
        for r in self.last_all_rows:
            if str(r.userid).isdigit():
                if pid_clean and r.participant_id and str(r.participant_id) == pid_clean:
                    return r
                if clean_target_name and names_match(r.fullname, clean_target_name):
                    return r

        first, last = split_fullname(clean_target_name)
        fi = first[0].upper() if first else ""
        li = last[0].upper() if last else ""

        # 2. Try grading table by (fi, li) initials first, then (fi, "") if surname splitting differed
        candidate_queries = [(fi, li)]
        if li:
            candidate_queries.append((fi, ""))
        if fi:
            candidate_queries.append(("", ""))

        for q_fi, q_li in candidate_queries:
            lookup_url = (
                f"{self.base_url}/mod/assign/view.php"
                f"?id={cmid}&action=grading&tifirst={q_fi}&tilast={q_li}&perpage=100"
            )
            assert_https_and_host(lookup_url, {"moodle.lmu.de"})
            try:
                resp = self.session.get(lookup_url, verify=self.verify_tls, timeout=20)
                if not resp.ok:
                    continue
                page_html = self._clear_sticky_grading_filters_if_needed(lookup_url, resp.text)
                rows = self.parse_grading_table_html(page_html)
                # Cache any numeric rows we discover
                existing_ids = {str(x.userid) for x in self.last_all_rows if str(x.userid).isdigit()}
                for r in rows:
                    if str(r.userid).isdigit() and str(r.userid) not in existing_ids:
                        self.last_all_rows.append(r)
                        existing_ids.add(str(r.userid))
                for r in rows:
                    if str(r.userid).isdigit():
                        if pid_clean and r.participant_id and str(r.participant_id) == pid_clean:
                            return r
                        if clean_target_name and names_match(r.fullname, clean_target_name):
                            return r
            except Exception:
                pass

        # 3. Fallback: Query /user/index.php?id=<course_id>&perpage=5000 (which includes unallocated partners
        #    excluded by assign::list_participants() when markingallocation is active).
        if course_id:
            part_rows = self.fetch_course_participants_rows(course_id)
            existing_ids = {str(x.userid) for x in self.last_all_rows if str(x.userid).isdigit()}
            for r in part_rows:
                if str(r.userid).isdigit() and str(r.userid) not in existing_ids:
                    self.last_all_rows.append(r)
                    existing_ids.add(str(r.userid))
            for r in part_rows:
                if str(r.userid).isdigit() and clean_target_name and names_match(r.fullname, clean_target_name):
                    return r

        return None

    def allocate_marker_to_users(
        self, cmid: int, userids: List[str], marker_id: Optional[str] = None
    ) -> bool:
        """Allocate the logged-in tutor as Bewerter (allocatedmarker) for the given Moodle userids.

        In Moodle 4.1 (mod/assign/locallib.php process_set_batch_marking_allocation),
        action='setbatchmarkingallocation' updates {assign_user_flags}.allocatedmarker for every
        userid in selectedusers without requiring mod/assign:manageallocations, so non-submitting
        partners immediately get Bewerter set AND become included in assign::list_participants().
        """
        if not self.sesskey:
            return False

        clean_uids: List[str] = []
        seen = set()
        for u in userids:
            u_str = str(u or "").strip()
            if u_str.isdigit() and int(u_str) > 0 and u_str not in seen:
                seen.add(u_str)
                clean_uids.append(u_str)

        if not clean_uids:
            return False

        resolved_marker = str(marker_id or self._resolve_logged_in_marker_id() or "").strip()
        if not resolved_marker.isdigit() or int(resolved_marker) <= 0:
            return False

        post_url = f"{self.base_url}/mod/assign/view.php"
        assert_https_and_host(post_url, {"moodle.lmu.de"})
        data = {
            "id": str(cmid),
            "action": "setbatchmarkingallocation",
            "selectedusers": ",".join(clean_uids),
            "allocatedmarker": resolved_marker,
            "sesskey": self.sesskey,
            "_qf__mod_assign_batch_set_allocatedmarker_form": "1",
            "submitbutton": "Save changes",
        }
        try:
            resp = self.session.post(post_url, data=data, verify=self.verify_tls, timeout=30)
            return bool(resp.ok)
        except Exception as exc:
            print(f"[Client] Notice: Batch marker allocation skipped: {exc}", flush=True)
            return False

    def upload_batch_feedback_pdfs(
        self, cmid: int, items: List[MarkedSubmissionItem]
    ) -> Tuple[int, List[MarkedSubmissionItem]]:
        """Attempt to upload feedback PDFs for multiple students in a single batch form.

        Returns:
            Tuple of (uploaded_count, remaining_items_not_uploaded)
        """
        if not items:
            return (0, [])

        uids = [str(it.record.userid) for it in items if it.record.userid]
        if not uids:
            return (0, items)

        users_param = ",".join(uids)
        batch_url = f"{self.base_url}/mod/assign/view.php"
        assert_https_and_host(batch_url, {"moodle.lmu.de"})

        params = {
            "id": str(cmid),
            "plugin": "file",
            "pluginsubtype": "assignfeedback",
            "action": "viewpluginpage",
            "pluginaction": "uploadfiles",
            "selectedusers": users_param,
            "users": users_param,
            "sesskey": self.sesskey or "",
        }
        resp = self.session.get(batch_url, params=params, verify=self.verify_tls, timeout=45)
        soup = BeautifulSoup(resp.text, "html.parser")
        form = self._find_grade_or_mform(soup)

        # Fallback: try POSTing gradingbatchoperation
        if not form:
            batch_op_data: List[Tuple[str, str]] = [
                ("id", str(cmid)),
                ("action", "gradingbatchoperation"),
                ("operation", "plugingradingbatchoperation_file_uploadfiles"),
                ("selectedusers", users_param),
                ("sesskey", self.sesskey or ""),
            ]
            for uid in uids:
                batch_op_data.append(("selectedusers", uid))
            resp_post = self.session.post(batch_url, data=batch_op_data, verify=self.verify_tls, timeout=45)
            if resp_post.ok:
                soup_post = BeautifulSoup(resp_post.text, "html.parser")
                form_post = self._find_grade_or_mform(soup_post)
                if form_post:
                    form = form_post
                    resp = resp_post
                    soup = soup_post

        if not form:
            return (0, items)

        uploaded_items: List[MarkedSubmissionItem] = []
        remaining_items: List[MarkedSubmissionItem] = []
        form_data = self._extract_form_inputs(form)

        for item in items:
            field_name = f"files_{item.record.userid}_filemanager"
            fm_inp = form.find("input", {"name": field_name})
            if not fm_inp or not fm_inp.get("value"):
                remaining_items.append(item)
                continue

            itemid, ctx_id, repo_id = self._extract_filemanager_meta(
                resp.text, re.escape(field_name)
            )
            if not itemid or not ctx_id:
                remaining_items.append(item)
                continue

            upload_name = f"{Path(item.record.filename).stem}_annotated.pdf"
            try:
                self.upload_draft_file(
                    filename=upload_name,
                    file_bytes=item.pdf_path.read_bytes(),
                    itemid=itemid,
                    ctx_id=ctx_id,
                    repo_id=repo_id,
                    mime_type="application/pdf",
                )
                form_data[field_name] = itemid
                uploaded_items.append(item)
            except Exception as exc:
                print(
                    f"[Client] Draft upload failed for {item.record.fullname}: {exc}",
                    flush=True,
                )
                remaining_items.append(item)

        if not uploaded_items:
            return (0, items)

        form_data["sendstudentnotifications"] = "0"
        sub_btn = form.find("input", {"type": "submit"}) or form.find("button", {"type": "submit"})
        if sub_btn and sub_btn.get("name"):
            form_data[sub_btn["name"]] = sub_btn.get("value", "Save changes")
        else:
            form_data["submitbutton"] = "Save changes"

        post_url = urljoin(resp.url, form.get("action", batch_url))
        assert_https_and_host(post_url, {"moodle.lmu.de"})
        post_resp = self.session.post(post_url, data=form_data, verify=self.verify_tls, timeout=60)
        post_resp.raise_for_status()

        return (len(uploaded_items), remaining_items)

    def upload_single_student_feedback_pdf(
        self, cmid: int, item: MarkedSubmissionItem
    ) -> bool:
        """Upload an individual annotated PDF directly to a specific student's submission."""
        if not self.sesskey:
            raise MoodleAuthError("Not logged in (missing sesskey).")
        if not item.pdf_path.exists():
            raise FileNotFoundError(f"Feedback PDF not found: {item.pdf_path}")

        if not str(item.record.userid).isdigit():
            resolved_row = self.resolve_student_userid_on_moodle(
                cmid,
                item.record.fullname,
                participant_id=item.record.participant_id,
            )
            if resolved_row and str(resolved_row.userid).isdigit():
                item.record.userid = str(resolved_row.userid)
                if resolved_row.participant_id and not item.record.participant_id:
                    item.record.participant_id = resolved_row.participant_id
                    item.record.moodle_folder = resolved_row.moodle_folder

        uid = str(item.record.userid).strip()
        file_bytes = item.pdf_path.read_bytes()
        upload_name = f"{Path(item.record.filename).stem}_annotated.pdf"
        batch_url = f"{self.base_url}/mod/assign/view.php"
        assert_https_and_host(batch_url, {"moodle.lmu.de"})

        # Method 1: Single-user viewpluginpage / uploadfiles.
        # Moodle 4.1's assign_feedback_file::view_batch_upload_files_page requires 'selectedusers'
        # and uses 'files_filemanager' in batchuploadfilesform.php (or 'files_{uid}_filemanager').
        params = {
            "id": str(cmid),
            "plugin": "file",
            "pluginsubtype": "assignfeedback",
            "action": "viewpluginpage",
            "pluginaction": "uploadfiles",
            "selectedusers": uid,
            "users": uid,
            "sesskey": self.sesskey,
        }
        resp = self.session.get(batch_url, params=params, verify=self.verify_tls, timeout=30)
        soup = BeautifulSoup(resp.text, "html.parser")
        form = self._find_grade_or_mform(soup)
        fm_pattern = re.compile(rf"^(?:files_{re.escape(uid)}_filemanager|files_filemanager)$")
        matched_inp = form.find("input", {"name": fm_pattern}) if form else None

        # Fallback Method 1b: POST to gradingbatchoperation
        if not matched_inp:
            batch_op_data = {
                "id": str(cmid),
                "action": "gradingbatchoperation",
                "operation": "plugingradingbatchoperation_file_uploadfiles",
                "selectedusers": uid,
                "sesskey": self.sesskey,
            }
            resp_op = self.session.post(batch_url, data=batch_op_data, verify=self.verify_tls, timeout=30)
            if resp_op.ok:
                soup_op = BeautifulSoup(resp_op.text, "html.parser")
                form_op = self._find_grade_or_mform(soup_op)
                inp_op = form_op.find("input", {"name": fm_pattern}) if form_op else None
                if form_op and inp_op:
                    form = form_op
                    matched_inp = inp_op
                    resp = resp_op

        if form and matched_inp:
            active_field_name = str(matched_inp["name"])
            itemid, ctx_id, repo_id = self._extract_filemanager_meta(
                resp.text, re.escape(active_field_name)
            )
            if itemid and ctx_id:
                self.upload_draft_file(
                    filename=upload_name,
                    file_bytes=file_bytes,
                    itemid=itemid,
                    ctx_id=ctx_id,
                    repo_id=repo_id,
                    mime_type="application/pdf",
                )
                form_data = self._extract_form_inputs(form)
                form_data[active_field_name] = itemid
                if "selectedusers" in form_data or active_field_name == "files_filemanager":
                    form_data["selectedusers"] = uid
                form_data["sendstudentnotifications"] = "0"
                sub = form.find("input", {"type": "submit"}) or form.find("button", {"type": "submit"})
                if sub and sub.get("name"):
                    form_data[sub["name"]] = sub.get("value", "Save changes")
                else:
                    form_data["submitbutton"] = "Save changes"
                post_url = urljoin(resp.url, form.get("action", batch_url))
                assert_https_and_host(post_url, {"moodle.lmu.de"})
                post_resp = self.session.post(
                    post_url, data=form_data, verify=self.verify_tls, timeout=60
                )
                post_resp.raise_for_status()
                return True

        # Method 2: Single-student direct grading page (action=grade -> action=submitgrade).
        # Note: Moodle 4.1 renders <form class="gradeform"> here rather than class="mform".
        grade_url = (
            f"{self.base_url}/mod/assign/view.php"
            f"?id={cmid}&action=grade&rownum=0&userid={uid}"
        )
        assert_https_and_host(grade_url, {"moodle.lmu.de"})
        resp_g = self.session.get(grade_url, verify=self.verify_tls, timeout=30)
        resp_g.raise_for_status()

        soup_g = BeautifulSoup(resp_g.text, "html.parser")
        form_g = self._find_grade_or_mform(soup_g)
        if form_g:
            fm_inp = form_g.find(
                "input",
                {"name": re.compile(r"(?:files|assignfeedback|feedbackfiles)(?:_\d+)?_filemanager")},
            )
            if fm_inp:
                fm_name = fm_inp["name"]
                itemid, ctx_id, repo_id = self._extract_filemanager_meta(
                    resp_g.text, re.escape(fm_name)
                )
                if itemid and ctx_id:
                    self.upload_draft_file(
                        filename=upload_name,
                        file_bytes=file_bytes,
                        itemid=itemid,
                        ctx_id=ctx_id,
                        repo_id=repo_id,
                        mime_type="application/pdf",
                    )
                    form_data = self._extract_form_inputs(form_g)
                    form_data["userid"] = uid
                    form_data["rownum"] = "0"
                    form_data["useridlistid"] = ""
                    form_data["action"] = "submitgrade"
                    form_data[fm_name] = itemid
                    form_data["sendstudentnotifications"] = "0"
                    if any("assignfeedbackcomments_editor" in k for k in form_data):
                        form_data.setdefault("assignfeedbackcomments_editor[text]", "")
                        form_data.setdefault("assignfeedbackcomments_editor[format]", "1")
                    if item.points is not None and str(item.points).strip() != "":
                        if form_g.find(["input", "select"], {"name": "grade"}):
                            form_data["grade"] = str(item.points).strip().replace(".", ",")
                    marker_id = self._resolve_logged_in_marker_id(soup_g)
                    if marker_id:
                        form_data["allocatedmarker"] = marker_id
                    for key, val in list(form_data.items()):
                        if "workflowstate" in key.lower() and str(val).strip().lower() == "released":
                            form_data[key] = "inmarking"
                    sub = form_g.find("input", {"name": re.compile(r"savegrade|submitbutton")}) or form_g.find(
                        "button", {"name": re.compile(r"savegrade|submitbutton")}
                    )
                    if sub and sub.get("name"):
                        form_data[sub["name"]] = sub.get("value", "Save changes")
                    else:
                        form_data["savegrade"] = "Save changes"

                    post_url = urljoin(resp_g.url, form_g.get("action", grade_url))
                    assert_https_and_host(post_url, {"moodle.lmu.de"})
                    post_resp = self.session.post(
                        post_url, data=form_data, verify=self.verify_tls, timeout=60
                    )
                    post_resp.raise_for_status()
                    return True

        print(
            f"[Client] Warning: Could not find feedback filemanager form for {item.record.fullname} (userid={uid}).",
            flush=True,
        )
        return False

    def _resolve_logged_in_marker_id(self, soup: Optional[BeautifulSoup] = None) -> str:
        """Return the Moodle userid of the logged-in tutor to set as the grader (Bewerter / allocatedmarker)."""
        if (not self.userid or not self.fullname) and soup is not None:
            self._extract_user_context(str(soup))
        if self.userid and str(self.userid).strip().isdigit() and int(str(self.userid).strip()) > 0:
            return str(self.userid).strip()
        if soup is not None:
            from pdf_processor import load_user_settings

            candidate_names = [
                n for n in (self.fullname, str(load_user_settings().get("marker") or "").strip()) if n
            ]
            for sel in soup.find_all("select", {"name": re.compile(r"allocatedmarker|markerfilter")}):
                for opt in sel.find_all("option"):
                    val = str(opt.get("value") or "").strip()
                    label = opt.get_text(" ", strip=True)
                    if val.isdigit() and int(val) > 0 and label:
                        if any(names_match(label, cname) for cname in candidate_names):
                            self.userid = val
                            return val
            # Check grading options form hidden userid input
            for f_tag in soup.find_all("form"):
                if f_tag.find("input", {"name": "_qf__mod_assign_grading_options_form"}):
                    u_inp = f_tag.find("input", {"name": "userid"})
                    val = str(u_inp.get("value") or "").strip() if u_inp else ""
                    if val.isdigit() and int(val) > 0:
                        self.userid = val
                        return val
            # Check hidden quickgrade_{uid}_allocatedmarker inputs on grading table
            for m_inp in soup.find_all(
                "input", {"name": re.compile(r"^quickgrade_\d+_allocatedmarker$")}
            ):
                val = str(m_inp.get("value") or "").strip()
                if val.isdigit() and int(val) > 0:
                    self.userid = val
                    return val
        return str(self.userid or "").strip()

    def upload_bulk_feedback_zip(self, cmid: int, zip_bytes: bytes) -> bool:
        """Upload a Moodle feedback ZIP via pluginaction=uploadzip and confirm the import."""
        zip_page_url = (
            f"{self.base_url}/mod/assign/view.php"
            f"?id={cmid}&plugin=file&pluginsubtype=assignfeedback"
            f"&action=viewpluginpage&pluginaction=uploadzip"
        )
        assert_https_and_host(zip_page_url, {"moodle.lmu.de"})
        resp = self.session.get(zip_page_url, verify=self.verify_tls, timeout=30)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        form = self._find_grade_or_mform(soup)
        if not form:
            raise MoodleScrapingError("Could not find Moodle uploadzip form.")

        itemid, ctx_id, repo_id = self._extract_filemanager_meta(
            resp.text, r"feedbackzip"
        )
        if not itemid or not ctx_id:
            raise MoodleScrapingError(
                "Could not extract draft itemid or ctx_id from uploadzip page."
            )

        self.upload_draft_file(
            filename="feedback_submissions.zip",
            file_bytes=zip_bytes,
            itemid=itemid,
            ctx_id=ctx_id,
            repo_id=repo_id,
            mime_type="application/zip",
        )

        step1_data = self._extract_form_inputs(form)
        step1_data["feedbackzip"] = itemid
        step1_data["sendstudentnotifications"] = "0"
        sub_btn = form.find("input", {"type": "submit"}) or form.find("button", {"type": "submit"})
        if sub_btn and sub_btn.get("name"):
            step1_data[sub_btn["name"]] = sub_btn.get("value", "Upload")
        else:
            step1_data["submitbutton"] = "Upload"

        action1 = urljoin(resp.url, form.get("action", f"{self.base_url}/mod/assign/view.php"))
        assert_https_and_host(action1, {"moodle.lmu.de"})

        resp2 = self.session.post(action1, data=step1_data, verify=self.verify_tls, timeout=60)
        resp2.raise_for_status()

        # Step 2: Confirm the ZIP import form if Moodle presents a confirmation screen
        soup2 = BeautifulSoup(resp2.text, "html.parser")
        confirm_form = self._find_grade_or_mform(soup2)
        if confirm_form and soup2.find(
            "input", {"name": re.compile(r"_qf__assignfeedback_file_import_zip_form")}
        ):
            confirm_data = self._extract_form_inputs(confirm_form)
            confirm_data["sendstudentnotifications"] = "0"
            sub_btn2 = confirm_form.find("input", {"type": "submit"}) or confirm_form.find("button", {"type": "submit"})
            if sub_btn2 and sub_btn2.get("name"):
                confirm_data[sub_btn2["name"]] = sub_btn2.get("value", "Confirm")
            else:
                confirm_data["submitbutton"] = "Confirm"

            action2 = urljoin(resp2.url, confirm_form.get("action", action1))
            assert_https_and_host(action2, {"moodle.lmu.de"})
            resp3 = self.session.post(
                action2, data=confirm_data, verify=self.verify_tls, timeout=60
            )
            resp3.raise_for_status()

        return True

    def submit_single_student_grade(
        self, cmid: int, item: MarkedSubmissionItem
    ) -> bool:
        """Enter numerical points and set the logged-in grader (allocatedmarker) for a single student."""
        if item.points is None or str(item.points).strip() == "":
            return False

        if not str(item.record.userid).isdigit():
            resolved_row = self.resolve_student_userid_on_moodle(
                cmid,
                item.record.fullname,
                participant_id=item.record.participant_id,
            )
            if resolved_row and str(resolved_row.userid).isdigit():
                item.record.userid = str(resolved_row.userid)
                if resolved_row.participant_id and not item.record.participant_id:
                    item.record.participant_id = resolved_row.participant_id
                if resolved_row.moodle_folder and not item.record.moodle_folder:
                    item.record.moodle_folder = resolved_row.moodle_folder

        uid = str(item.record.userid).strip()
        if not uid.isdigit() or int(uid) <= 0:
            print(
                f"[Client] Warning: Could not resolve numeric Moodle userid for {item.record.fullname} "
                f"(current id={uid}). Cannot submit grade.",
                flush=True,
            )
            return False

        formatted_pts = str(item.points).strip().replace(".", ",")
        graded_via_single_form = False
        marker_set_via_single_form = False

        # Step 1: Single-student grade form (action=grade -> action=submitgrade).
        # Visiting this page calls get_user_grade($userid, true, -1) in Moodle, which creates
        # the {assign_submission} (latest=1) and {assign_grades} rows for non-submitting partners.
        # Note: Moodle 4.1 renders <form class="gradeform"> here rather than class="mform".
        grade_url = (
            f"{self.base_url}/mod/assign/view.php"
            f"?id={cmid}&action=grade&rownum=0&userid={uid}"
        )
        assert_https_and_host(grade_url, {"moodle.lmu.de"})
        try:
            resp_g = self.session.get(grade_url, verify=self.verify_tls, timeout=30)
            if resp_g.ok:
                if not self.userid or not self.fullname:
                    self._extract_user_context(resp_g.text)
                soup_g = BeautifulSoup(resp_g.text, "html.parser")
                form_g = self._find_grade_or_mform(soup_g)
                if form_g and form_g.find(["input", "select"], {"name": "grade"}) is not None:
                    form_uid_inp = form_g.find("input", {"name": "userid"})
                    form_uid_val = (
                        str(form_uid_inp.get("value") or "").strip() if form_uid_inp else ""
                    )
                    qf_inp = form_g.find(
                        "input", {"name": re.compile(r"^_qf__mod_assign_grade_form_")}
                    )
                    has_real_qf_marker = qf_inp is not None
                    if not form_uid_val or form_uid_val == uid:
                        form_data = self._extract_form_inputs(form_g)
                        # Ensure Moodle's add_grade_form_elements does not override userid with useridlist[rownum]
                        form_data["userid"] = uid
                        form_data["rownum"] = "0"
                        form_data["useridlistid"] = ""
                        form_data["action"] = "submitgrade"
                        form_data["grade"] = formatted_pts
                        form_data["sendstudentnotifications"] = "0"
                        if any("assignfeedbackcomments_editor" in k for k in form_data):
                            form_data.setdefault("assignfeedbackcomments_editor[text]", "")
                            form_data.setdefault("assignfeedbackcomments_editor[format]", "1")
                        marker_id = self._resolve_logged_in_marker_id(soup_g)
                        if marker_id:
                            form_data["allocatedmarker"] = marker_id
                            if form_g.find(["select", "input"], {"name": "allocatedmarker"}) is not None:
                                marker_set_via_single_form = True
                        for key, val in list(form_data.items()):
                            if "workflowstate" in key.lower() and str(val).strip().lower() == "released":
                                form_data[key] = "inmarking"

                        sub = form_g.find("input", {"name": re.compile(r"savegrade|submitbutton")}) or form_g.find(
                            "button", {"name": re.compile(r"savegrade|submitbutton")}
                        )
                        if sub and sub.get("name"):
                            form_data[sub["name"]] = sub.get("value", "Save changes")
                        else:
                            form_data["savegrade"] = "Save changes"

                        post_url = urljoin(resp_g.url, form_g.get("action", grade_url))
                        assert_https_and_host(post_url, {"moodle.lmu.de"})
                        post_resp = self.session.post(
                            post_url, data=form_data, verify=self.verify_tls, timeout=60
                        )
                        post_resp.raise_for_status()
                        resp_lower = post_resp.text.lower()
                        has_err = any(
                            e in resp_lower
                            for e in (
                                "form-control-feedback",
                                "has-danger",
                                "errorgrade",
                                "errorrecordmodified",
                            )
                        )
                        is_saved = (
                            "savegradingresult" in post_resp.url
                            or "savegradingresult" in resp_lower
                            or "gradingchangessaved" in resp_lower
                            or "wurde gespeichert" in resp_lower
                        )
                        if is_saved or not has_err:
                            graded_via_single_form = True
                            if marker_set_via_single_form or not has_real_qf_marker:
                                return True
        except Exception as exc:
            print(
                f"[Client] Notice: Direct grade form for {item.record.fullname} (uid={uid}) fell back to quickgrade: {exc}",
                flush=True,
            )

        # Step 2: Initials-filtered Quick Grading table (action=quickgrade).
        # In Moodle's locallib.php (process_save_quick_grades), quickgrade_{uid}_allocatedmarker
        # updates $flags->allocatedmarker even when the tutor lacks mod/assign:manageallocations.
        from pdf_processor import split_fullname

        first, last = split_fullname(item.record.fullname)
        fi = first[0].upper() if first else ""
        li = last[0].upper() if last else ""
        qg_url = (
            f"{self.base_url}/mod/assign/view.php"
            f"?id={cmid}&action=grading&tifirst={fi}&tilast={li}&perpage=100"
        )
        assert_https_and_host(qg_url, {"moodle.lmu.de"})
        resp_qg = self.session.get(qg_url, verify=self.verify_tls, timeout=30)
        if not resp_qg.ok:
            return graded_via_single_form
        page_html = self._clear_sticky_grading_filters_if_needed(
            qg_url, resp_qg.text, ensure_quickgrade=True
        )
        soup_qg = BeautifulSoup(page_html, "html.parser")
        marker_id = self._resolve_logged_in_marker_id(soup_qg)

        def _locate_qg_form_and_row(s_qg: BeautifulSoup) -> Tuple[Optional[Any], Dict[str, str], bool]:
            q_form = None
            for f_tag in s_qg.find_all("form"):
                if f_tag.find("input", {"name": f"quickgrade_{uid}"}) or f_tag.find(
                    "input", {"name": "savequickgrades"}
                ):
                    q_form = f_tag
                    break
            if not q_form:
                return (None, {}, False)
            q_payload = self._extract_form_inputs(q_form)
            q_field = f"quickgrade_{uid}"
            has_row = (
                q_field in q_payload
                or q_form.find(["input", "select"], {"name": q_field}) is not None
                or q_form.find(["input", "select"], {"name": f"quickgrade_{uid}_allocatedmarker"}) is not None
            )
            return (q_form, q_payload, has_row)

        qg_form, payload, has_qg_row = _locate_qg_form_and_row(soup_qg)

        # If the partner was not yet in assign::list_participants() because uf.allocatedmarker != marker_id,
        # allocate the partner via setbatchmarkingallocation and re-fetch the quickgrade page.
        if not has_qg_row and marker_id:
            if self.allocate_marker_to_users(cmid, [uid], marker_id=marker_id):
                resp_qg2 = self.session.get(qg_url, verify=self.verify_tls, timeout=30)
                if resp_qg2.ok:
                    soup_qg = BeautifulSoup(resp_qg2.text, "html.parser")
                    qg_form, payload, has_qg_row = _locate_qg_form_and_row(soup_qg)

        # Fallback to unfiltered grading table if initials filter didn't match the student
        if not has_qg_row and (fi or li):
            unfiltered_url = (
                f"{self.base_url}/mod/assign/view.php"
                f"?id={cmid}&action=grading&tifirst=&tilast=&perpage=100"
            )
            assert_https_and_host(unfiltered_url, {"moodle.lmu.de"})
            resp_unf = self.session.get(unfiltered_url, verify=self.verify_tls, timeout=30)
            if resp_unf.ok:
                soup_unf = BeautifulSoup(resp_unf.text, "html.parser")
                cand_form, cand_payload, cand_has_row = _locate_qg_form_and_row(soup_unf)
                if cand_has_row:
                    qg_form, payload, has_qg_row = cand_form, cand_payload, cand_has_row
                    qg_url = unfiltered_url

        if not qg_form or not has_qg_row:
            return graded_via_single_form

        qg_field = f"quickgrade_{uid}"
        for key, val in list(payload.items()):
            if "workflowstate" in key.lower() and str(val).strip().lower() == "released":
                payload[key] = "inmarking"
        payload[qg_field] = formatted_pts
        old_gm_str = str(payload.get(f"grademodified_{uid}") or "0")
        old_gm = int(old_gm_str) if old_gm_str.isdigit() else 0
        payload[f"grademodified_{uid}"] = str(max(old_gm + 1, int(time.time()) + 300))
        if not str(payload.get(f"gradeattempt_{uid}") or "").strip():
            payload[f"gradeattempt_{uid}"] = "0"
        if marker_id:
            # Moodle's gradingtable.php & locallib.php use quickgrade_{userid}_allocatedmarker
            payload[f"quickgrade_{uid}_allocatedmarker"] = marker_id
            payload[f"quickgrade_allocatedmarker_{uid}"] = marker_id
            if f"allocatedmarker_{uid}" in payload or qg_form.find(
                ["input", "select"], {"name": f"allocatedmarker_{uid}"}
            ):
                payload[f"allocatedmarker_{uid}"] = marker_id
        for wf_key in (f"quickgrade_{uid}_workflowstate", f"quickgrade_workflowstate_{uid}"):
            if wf_key in payload and payload[wf_key].strip().lower() == "released":
                payload[wf_key] = "inmarking"
        payload["sendstudentnotifications"] = "0"
        save_btn = qg_form.find("input", {"name": "savequickgrades"})
        payload["savequickgrades"] = (
            save_btn["value"]
            if (save_btn and save_btn.get("value"))
            else "Save all quick grading changes"
        )
        if not payload.get("action"):
            payload["action"] = "quickgrade"
        post_url = urljoin(qg_url, qg_form.get("action", qg_url))
        assert_https_and_host(post_url, {"moodle.lmu.de"})
        post_resp = self.session.post(post_url, data=payload, verify=self.verify_tls, timeout=60)
        post_resp.raise_for_status()
        return True

    def submit_grades_to_grading_table(
        self, cmid: int, items: List[MarkedSubmissionItem]
    ) -> int:
        """Enter numerical points and set the logged-in grader for the given students in Moodle.

        Enforces that student notifications are disabled (sendstudentnotifications='0'),
        sets allocatedmarker (Bewerter) to the logged-in user for both students,
        and ensures no submission workflow state is set to 'released'.
        Automatically falls back to per-student grading for any student or partner
        not present on the first page of the quickgrade form (or any partner whose
        submission/grade row must be initialized first).
        """
        raw_with_points = [
            it for it in items if it.points is not None and str(it.points).strip() != ""
        ]
        if not raw_with_points:
            return 0

        # Resolve any non-numeric ("part_...") userids before deduplicating and grading
        for it in raw_with_points:
            if not str(it.record.userid).isdigit():
                resolved_row = self.resolve_student_userid_on_moodle(
                    cmid,
                    it.record.fullname,
                    participant_id=it.record.participant_id,
                )
                if resolved_row and str(resolved_row.userid).isdigit():
                    it.record.userid = str(resolved_row.userid)
                    if resolved_row.participant_id and not it.record.participant_id:
                        it.record.participant_id = resolved_row.participant_id
                    if resolved_row.moodle_folder and not it.record.moodle_folder:
                        it.record.moodle_folder = resolved_row.moodle_folder

        # Deduplicate by userid while preserving order
        items_with_points: List[MarkedSubmissionItem] = []
        seen_uids = set()
        for it in raw_with_points:
            uid_str = str(it.record.userid)
            if uid_str not in seen_uids:
                seen_uids.add(uid_str)
                items_with_points.append(it)

        grading_url = (
            f"{self.base_url}/mod/assign/view.php"
            f"?id={cmid}&action=grading&tifirst=&tilast=&perpage=100"
        )
        assert_https_and_host(grading_url, {"moodle.lmu.de"})
        resp = self.session.get(grading_url, verify=self.verify_tls, timeout=30)
        resp.raise_for_status()

        if not self.userid or not self.fullname:
            self._extract_user_context(resp.text)

        # Clear any sticky filters (filter/markerfilter/workflowfilter) and ensure quickgrading is enabled
        page_html = self._clear_sticky_grading_filters_if_needed(
            grading_url, resp.text, ensure_quickgrade=True
        )
        soup = BeautifulSoup(page_html, "html.parser")
        if not self.userid or not self.fullname:
            self._extract_user_context(page_html)

        grading_form = None
        for f_tag in soup.find_all("form"):
            if f_tag.find("input", {"name": re.compile(r"^quickgrade_\d+$")}) or f_tag.find(
                "input", {"name": "savequickgrades"}
            ):
                grading_form = f_tag
                break

        if not grading_form:
            raise MoodleScrapingError(
                "Could not find the Quick Grading table form on Moodle."
            )

        marker_id = self._resolve_logged_in_marker_id(soup)
        payload = self._extract_form_inputs(grading_form)
        now_ts = int(time.time()) + 300
        updated_count = 0
        missing_from_page: List[MarkedSubmissionItem] = []

        # Remove other students' fields from payload to avoid lock conflicts
        target_uids = {str(it.record.userid) for it in items_with_points}
        for key in list(payload.keys()):
            m_uid = re.search(r"\b(\d+)\b", key)
            if m_uid and any(
                p in key
                for p in (
                    "quickgrade_",
                    "grademodified_",
                    "gradeattempt_",
                    "allocatedmarker_",
                )
            ):
                if m_uid.group(1) not in target_uids:
                    payload.pop(key, None)

        # Ensure no workflow state in the payload is set to 'released'
        for key, val in list(payload.items()):
            if "workflowstate" in key.lower() and str(val).strip().lower() == "released":
                payload[key] = "inmarking"

        for item in items_with_points:
            uid = str(item.record.userid)
            qg_field = f"quickgrade_{uid}"
            has_field_in_form = qg_field in payload or bool(
                grading_form.find(["input", "select"], {"name": qg_field})
            )
            # If the student or partner is not on the current grading table page,
            # route them to individual submission (which visits action=grade / initializes attempt)
            if not has_field_in_form:
                missing_from_page.append(item)
                continue

            # German Moodle uses comma as decimal separator in grading inputs
            formatted_pts = str(item.points).strip().replace(".", ",")
            payload[qg_field] = formatted_pts
            old_gm_str = str(payload.get(f"grademodified_{uid}") or "0")
            old_gm = int(old_gm_str) if old_gm_str.isdigit() else 0
            payload[f"grademodified_{uid}"] = str(max(old_gm + 1, now_ts))
            if not str(payload.get(f"gradeattempt_{uid}") or "").strip():
                payload[f"gradeattempt_{uid}"] = "0"
            if marker_id:
                # Set Moodle's exact field name (quickgrade_{uid}_allocatedmarker) as well as legacy alias
                payload[f"quickgrade_{uid}_allocatedmarker"] = marker_id
                payload[f"quickgrade_allocatedmarker_{uid}"] = marker_id
                if f"allocatedmarker_{uid}" in payload or grading_form.find(
                    ["input", "select"], {"name": f"allocatedmarker_{uid}"}
                ):
                    payload[f"allocatedmarker_{uid}"] = marker_id
            for wf_key in (f"quickgrade_{uid}_workflowstate", f"quickgrade_workflowstate_{uid}"):
                if wf_key in payload and payload[wf_key].strip().lower() == "released":
                    payload[wf_key] = "inmarking"
            updated_count += 1

        quickgrade_failed = False
        if updated_count > 0:
            payload["sendstudentnotifications"] = "0"
            save_btn = grading_form.find("input", {"name": "savequickgrades"})
            if save_btn and save_btn.get("value"):
                payload["savequickgrades"] = save_btn["value"]
            else:
                payload["savequickgrades"] = "Save all quick grading changes"
            if "action" not in payload or not payload["action"]:
                payload["action"] = "quickgrade"

            post_url = urljoin(resp.url, grading_form.get("action", grading_url))
            assert_https_and_host(post_url, {"moodle.lmu.de"})
            try:
                post_resp = self.session.post(post_url, data=payload, verify=self.verify_tls, timeout=60)
                post_resp.raise_for_status()
                resp_lower = post_resp.text.lower()
                if "errorrecordmodified" in resp_lower or ("geändert" in resp_lower and "fehler" in resp_lower):
                    quickgrade_failed = True
            except Exception as qg_exc:
                print(
                    f"[Client] Notice: Quickgrade batch post failed ({qg_exc}). Falling back to per-student grading.",
                    flush=True,
                )
                quickgrade_failed = True

        if quickgrade_failed:
            updated_count = 0
            for item in items_with_points:
                if self.submit_single_student_grade(cmid, item):
                    updated_count += 1
        else:
            for item in missing_from_page:
                if self.submit_single_student_grade(cmid, item):
                    updated_count += 1

        return updated_count
