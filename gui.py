#!/usr/bin/env python3
"""Local loopback iPad GUI for LMU Moodle and Notability grading automation."""

from __future__ import annotations

import argparse
import html
import json
import os
import secrets
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

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
    forget_confirmed_partner,
    format_student_pdf_name,
    get_excluded_partner_names,
    load_manifest,
    load_manifest_with_roster,
    load_user_settings,
    match_marked_pdfs,
    merge_pdf_bytes_list,
    read_grades_csv,
    remember_confirmed_partner,
    resolve_partner_by_query,
    resolve_storage_path,
    save_manifest,
    save_user_settings,
    split_fullname,
    sync_grades_csv,
    update_grades_csv_entries,
)


INDEX_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="color-scheme" content="light dark">
  <title>LMU Moodle Grading Assistant</title>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js" defer></script>
  <script src="https://cdn.jsdelivr.net/npm/tesseract.js@5/dist/tesseract.min.js" defer></script>
  <style>
    @layer base, theme, layout, components, utilities;

    @layer theme {
      :root {
        /* Bright, approachable LMU Moodle palette (white & LMU green #00883A) */
        --brand-light: #00883A;
        --brand-dark: #00883A;
        --brand-ink-light: #ffffff;
        --brand-ink-dark: #ffffff;

        --canvas-light: #f4f6f8;
        --canvas-dark: #f4f6f8;
        --surface-light: #ffffff;
        --surface-dark: #ffffff;
        --surface-subtle-light: #edf4ef;
        --surface-subtle-dark: #edf4ef;

        --text-main-light: #1e293b;
        --text-main-dark: #1e293b;
        --text-muted-light: #475569;
        --text-muted-dark: #475569;

        --border-light: #cbd5e1;
        --border-dark: #cbd5e1;

        --ok-bg-light: #e6f4ec;
        --ok-bg-dark: #e6f4ec;
        --ok-text-light: #00662b;
        --ok-text-dark: #00662b;

        --warn-bg-light: #fef3c7;
        --warn-bg-dark: #fef3c7;
        --warn-text-light: #92400e;
        --warn-text-dark: #92400e;

        --err-bg-light: #fee2e2;
        --err-bg-dark: #fee2e2;
        --err-text-light: #991b1b;
        --err-text-dark: #991b1b;

        --scrollbar-track-light: #e2e8f0;
        --scrollbar-track-dark: #e2e8f0;
        --scrollbar-thumb-light: #94a3b8;
        --scrollbar-thumb-dark: #94a3b8;

        --color-brand: var(--brand-light);
        --color-brand-ink: var(--brand-ink-light);
        --color-canvas: var(--canvas-light);
        --color-surface: var(--surface-light);
        --color-surface-subtle: var(--surface-subtle-light);
        --color-text: var(--text-main-light);
        --color-text-muted: var(--text-muted-light);
        --color-border: var(--border-light);
        --color-ok-bg: var(--ok-bg-light);
        --color-ok-text: var(--ok-text-light);
        --color-warn-bg: var(--warn-bg-light);
        --color-warn-text: var(--warn-text-light);
        --color-err-bg: var(--err-bg-light);
        --color-err-text: var(--err-text-light);
        --scrollbar-track: var(--scrollbar-track-light);
        --scrollbar-thumb: var(--scrollbar-thumb-light);

        color-scheme: light;
        accent-color: var(--color-brand);
        scrollbar-color: var(--scrollbar-thumb) var(--scrollbar-track);
        --radius-card: 12px;
        --radius-control: 8px;
      }

      @media (forced-colors: active) {
        :root {
          --color-border: CanvasText;
          --color-brand: Highlight;
          --color-brand-ink: HighlightText;
        }
      }
    }

    @layer base {
      :where(html, body, div, section, header, main, form, table, th, td, input, button, select, dialog) {
        box-sizing: border-box;
      }

      body {
        margin: 0;
        background-color: var(--color-canvas);
        color: var(--color-text);
        font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", Roboto, sans-serif;
        line-height: 1.5;
        -webkit-font-smoothing: antialiased;
      }

      @supports not (scrollbar-color: auto) {
        .table-wrapper::-webkit-scrollbar {
          width: 10px;
          height: 10px;
        }
        .table-wrapper::-webkit-scrollbar-thumb {
          background: var(--scrollbar-thumb);
        }
        .table-wrapper::-webkit-scrollbar-track {
          background: var(--scrollbar-track);
        }
      }
    }

    @layer layout {
      .app-shell {
        max-inline-size: 1220px;
        margin-inline: auto;
        padding-inline: clamp(1rem, 3cqw, 2rem);
        padding-block: 1.25rem 3rem;
        display: grid;
        gap: 1.25rem;
      }

      .top-bar {
        display: flex;
        flex-wrap: wrap;
        align-items: safe center;
        justify-content: space-between;
        gap: 1rem;
        padding-inline: 1.25rem;
        padding-block: 1rem;
        background-color: var(--color-brand);
        color: var(--color-brand-ink);
        border-radius: var(--radius-card);
        box-shadow: 0 2px 8px rgba(0, 136, 58, 0.14);
      }

      .section-bar {
        display: flex;
        flex-wrap: wrap;
        align-items: safe center;
        justify-content: space-between;
        gap: 1rem;
        padding-block-end: 0.5rem;
      }

      .card-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(310px, 1fr));
        gap: 1.25rem;
      }
    }

    @layer components {
      .card {
        background-color: var(--color-surface);
        border: 1px solid var(--color-border);
        border-block-start: 4px solid var(--color-brand);
        border-radius: var(--radius-card);
        padding-inline: 1.25rem;
        padding-block: 1.25rem;
        box-shadow: 0 1px 4px rgba(15, 23, 42, 0.05);
      }

      .card-title {
        margin-block: 0 0.35rem;
        font-size: 1.125rem;
        font-weight: 650;
        color: #0f172a;
      }

      .card-subtitle {
        margin-block: 0 1rem;
        font-size: 0.9rem;
        color: var(--color-text-muted);
      }

      .form-stack {
        display: grid;
        gap: 0.85rem;
      }

      .field-group {
        display: grid;
        gap: 0.35rem;
      }

      .field-row {
        display: flex;
        flex-wrap: wrap;
        gap: 0.75rem;
        align-items: flex-end;
      }

      .field-row > .field-group {
        flex: 1 1 160px;
      }

      label {
        font-size: 0.875rem;
        font-weight: 600;
      }

      input[type="text"],
      input[type="password"],
      input[type="number"],
      select {
        inline-size: 100%;
        min-block-size: 44px;
        padding-inline: 0.75rem;
        padding-block: 0.5rem;
        border: 1.5px solid var(--color-border);
        border-radius: var(--radius-control);
        background-color: var(--color-surface);
        color: var(--color-text);
        font-size: 1rem;
      }

      input:focus-visible,
      button:focus-visible,
      select:focus-visible {
        outline: 3px solid var(--color-brand);
        outline-offset: 2px;
      }

      .password-wrapper {
        display: flex;
        gap: 0.5rem;
        align-items: stretch;
      }

      .button-row {
        display: flex;
        flex-wrap: wrap;
        gap: 0.75rem;
        align-items: safe center;
        margin-block-start: 0.5rem;
      }

      button {
        min-block-size: 44px;
        padding-inline: 1rem;
        padding-block: 0.55rem;
        border-radius: var(--radius-control);
        border: 1.5px solid var(--color-border);
        background-color: #f8fafc;
        color: var(--color-text);
        font-size: 0.95rem;
        font-weight: 600;
        cursor: pointer;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        gap: 0.45rem;
      }

      button.primary {
        background-color: var(--color-brand);
        color: var(--color-brand-ink);
        border-color: var(--color-brand);
      }

      button:hover:not(:disabled) {
        filter: brightness(0.96);
      }

      button:disabled {
        opacity: 0.55;
        cursor: not-allowed;
      }

      .badge {
        display: inline-flex;
        align-items: center;
        gap: 0.35rem;
        padding-inline: 0.65rem;
        padding-block: 0.25rem;
        border-radius: 999px;
        font-size: 0.82rem;
        font-weight: 600;
        border: 1px solid var(--color-border);
        background-color: var(--color-surface);
        color: var(--color-text);
      }

      .badge.ok {
        background-color: var(--color-ok-bg);
        color: var(--color-ok-text);
        border-color: #a7f3d0;
      }

      .badge.warn {
        background-color: var(--color-warn-bg);
        color: var(--color-warn-text);
        border-color: #fde68a;
      }

      .badge.err {
        background-color: var(--color-err-bg);
        color: var(--color-err-text);
        border-color: #fecaca;
      }

      .banner {
        padding-inline: 1rem;
        padding-block: 0.85rem;
        border-radius: var(--radius-control);
        border: 1.5px solid var(--color-border);
        font-size: 0.92rem;
        display: flex;
        align-items: center;
        gap: 0.6rem;
      }

      .banner.ok {
        background-color: var(--color-ok-bg);
        color: var(--color-ok-text);
        border-color: var(--color-brand);
      }

      .banner.err {
        background-color: var(--color-err-bg);
        color: var(--color-err-text);
      }

      .safety-note {
        background-color: var(--color-surface-subtle);
        border: 1px solid #b7e0c7;
        border-inline-start: 4px solid var(--color-brand);
        border-radius: var(--radius-control);
        padding-inline: 0.9rem;
        padding-block: 0.7rem;
        font-size: 0.875rem;
      }

      /* Responsive Table with Sticky Headers and Container Query Stacked Cards */
      .table-wrapper {
        overflow: auto;
        max-inline-size: 100%;
        max-block-size: min(680px, 78dvh);
        container-type: inline-size;
        border: 1px solid var(--color-border);
        border-radius: var(--radius-control);
      }

      table {
        inline-size: 100%;
        border-collapse: separate;
        border-spacing: 0;
        --label-2: "Partner (2nd Student)";
        --label-3: "Target File";
        --label-4: "Notability Status";
        --label-5: "Points";
      }

      caption {
        text-align: start;
        padding-inline: 1rem;
        padding-block: 0.75rem;
        font-weight: 650;
        background-color: var(--color-surface-subtle);
        border-block-end: 1px solid var(--color-border);
      }

      thead {
        position: sticky;
        inset-block-start: 0;
        z-index: 2;
        background-color: var(--color-surface-subtle);
      }

      th, td {
        padding-inline: 0.85rem;
        padding-block: 0.7rem;
        text-align: start;
        vertical-align: top;
      }

      tbody tr:not(:last-child) th,
      tbody tr:not(:last-child) td {
        border-block-end: 1px solid var(--color-border);
      }

      thead th {
        border-block-end: 1.5px solid var(--color-border);
        font-size: 0.82rem;
        text-transform: uppercase;
        letter-spacing: 0.03em;
        color: #00662b;
      }

      tbody th {
        position: sticky;
        inset-inline-start: 0;
        z-index: 1;
        background-color: var(--color-surface);
        font-weight: 600;
      }

      tbody tr:has(.points-input:not(:placeholder-shown)) th,
      tbody tr:has(.points-input:not(:placeholder-shown)) td {
        background-color: var(--color-surface-subtle);
      }

      .points-input {
        max-inline-size: 115px;
      }

      .partner-input {
        min-inline-size: 240px;
      }

      .partner-meta {
        display: block;
        font-size: 0.76rem;
        color: #00662b;
        font-weight: 600;
        margin-block-start: 0.25rem;
      }

      .partner-meta.review {
        color: #92400e;
      }

      .candidate-chips {
        display: flex;
        flex-wrap: wrap;
        gap: 0.3rem;
        margin-block-start: 0.35rem;
      }

      .chip-btn {
        min-block-size: 26px;
        padding-inline: 0.5rem;
        padding-block: 0.15rem;
        font-size: 0.74rem;
        border-radius: 999px;
        background-color: #f1f5f9;
        border: 1px solid #cbd5e1;
        color: #334155;
      }

      .chip-btn:hover {
        background-color: #e6f4ec;
        border-color: var(--color-brand);
        color: #00662b;
      }

      .header-preview-scroll {
        display: block;
        max-inline-size: 400px;
        inline-size: 100%;
        max-block-size: 120px;
        overflow-y: auto;
        overflow-x: auto;
        -webkit-overflow-scrolling: touch;
        overscroll-behavior: contain;
        border: 1px solid #cbd5e1;
        border-radius: 6px;
        margin-block-bottom: 0.4rem;
        background-color: #ffffff;
        cursor: zoom-in;
      }

      .header-preview-scroll.expanded {
        max-inline-size: 580px;
        max-block-size: 380px;
        cursor: zoom-out;
      }

      .header-preview-img {
        display: block;
        inline-size: 100%;
        block-size: auto;
        background-color: #ffffff;
      }

      .toggle-pill {
        display: inline-flex;
        align-items: center;
        gap: 0.45rem;
        min-block-size: 42px;
        padding-inline: 0.85rem;
        padding-block: 0.35rem;
        border-radius: var(--radius-control);
        border: 1.5px solid var(--color-border);
        background-color: var(--color-surface-subtle);
        font-size: 0.88rem;
        font-weight: 600;
        cursor: pointer;
        user-select: none;
      }

      .toggle-pill input[type="checkbox"] {
        min-block-size: 18px;
        inline-size: 18px;
        block-size: 18px;
        margin: 0;
        accent-color: var(--color-brand);
        cursor: pointer;
      }

      @container (width < 680px) {
        table, thead, tbody, tr, th, td {
          display: block;
        }

        thead {
          display: none;
        }

        tbody tr {
          margin-block-end: 1rem;
          border: 1px solid var(--color-border);
          border-radius: var(--radius-control);
        }

        tbody th {
          position: sticky;
          inset-block-start: 0;
          z-index: 2;
          background-color: var(--color-surface-subtle);
          border-block-end: 1px solid var(--color-border);
        }

        td:nth-child(2)::before {
          content: var(--label-2) ": ";
          font-weight: 600;
        }

        td:nth-child(3)::before {
          content: var(--label-3) ": ";
          font-weight: 600;
        }

        td:nth-child(4)::before {
          content: var(--label-4) ": ";
          font-weight: 600;
        }

        td:nth-child(5)::before {
          content: var(--label-5) ": ";
          font-weight: 600;
        }
      }

      dialog {
        max-inline-size: min(680px, 92vw);
        inline-size: 100%;
        border: 1.5px solid var(--color-border);
        border-block-start: 5px solid var(--color-brand);
        border-radius: var(--radius-card);
        background-color: var(--color-surface);
        color: var(--color-text);
        padding: 1.5rem;
      }

      dialog::backdrop {
        background-color: rgba(15, 23, 42, 0.45);
        backdrop-filter: blur(2px);
      }
    }

    @layer utilities {
      .mono {
        font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
        font-size: 0.88em;
      }
      .hidden {
        display: none !important;
      }
    }
  </style>
</head>
<body>
  <main class="app-shell">
    <header class="top-bar">
      <div>
        <h1 style="margin: 0; font-size: 1.35rem; color: #ffffff;">LMU Moodle • Analysis 1 Grading Assistant</h1>
        <p style="margin: 0.2rem 0 0; font-size: 0.9rem; color: rgba(255, 255, 255, 0.9);">
          Course 48957 • Pair Submissions (2 Students) &amp; Notability Workflow
        </p>
      </div>
      <div style="display: flex; gap: 0.6rem; align-items: center; flex-wrap: wrap;">
        <div id="roster-count-badge" class="badge" title="Total students loaded from the Ana 1 course roster">
          👥 Roster: 0 students
        </div>
        <div id="auth-status-badge" class="badge warn" role="status">
          <span aria-hidden="true">🔒</span>
          <span id="auth-status-text">Not Signed In</span>
        </div>
      </div>
    </header>

    <div id="feedback-banner" class="banner hidden" role="alert">
      <span id="feedback-icon" aria-hidden="true">ℹ️</span>
      <span id="feedback-message"></span>
    </div>

    <div class="card-grid">
      <!-- Sign-in Form Card (placed at top so mobile keyboard never covers Sign In button) -->
      <section class="card" aria-labelledby="login-card-title">
        <h2 id="login-card-title" class="card-title">1. LMU Moodle Sign-In</h2>
        <p class="card-subtitle">
          Tap the password field to AutoFill from Apple Passwords. Credentials stay in memory only.
        </p>
        <form id="login-form" class="form-stack">
          <div class="field-group">
            <label for="lmu-username">LMU Username</label>
            <input
              type="text"
              id="lmu-username"
              name="username"
              autocomplete="username"
              spellcheck="false"
              autocorrect="off"
              autocapitalize="none"
              enterkeyhint="next"
              placeholder="ab12cdef@campus.lmu.de"
              required
            >
          </div>
          <div class="field-group">
            <label for="current-password">LMU Password</label>
            <div class="password-wrapper">
              <input
                type="password"
                id="current-password"
                name="password"
                autocomplete="current-password"
                enterkeyhint="done"
                required
              >
              <button type="button" id="toggle-password-btn" aria-label="Show password">
                Show
              </button>
            </div>
          </div>
          <div class="button-row">
            <button type="submit" id="login-submit-btn" class="primary">
              Sign In to LMU Moodle
            </button>
          </div>
        </form>
      </section>

      <!-- Sheet & iCloud Storage Configuration Card -->
      <section class="card" aria-labelledby="sheet-card-title">
        <h2 id="sheet-card-title" class="card-title">2. Exercise Sheet &amp; Storage Folder</h2>
        <p class="card-subtitle">
          Saves PDFs as <span class="mono">JDoe_sheet2.pdf</span> and matches group partners from the full Ana 1 course roster.
        </p>
        <form id="settings-form" class="form-stack">
          <div class="field-row">
            <div class="field-group">
              <label for="sheet-number">Übungsblatt Number</label>
              <input type="number" id="sheet-number" name="sheet" min="1" max="30" value="1" required>
            </div>
            <div class="field-group">
              <label for="course-id">Moodle Course ID</label>
              <input type="number" id="course-id" name="course_id" value="48957" required>
            </div>
          </div>
          <div class="field-group">
            <label for="marker-input">Bewerter Name in Moodle (Optional Override)</label>
            <input
              type="text"
              id="marker-input"
              name="marker"
              spellcheck="false"
              autocorrect="off"
              placeholder="Auto-detected from your Moodle profile"
            >
          </div>
          <div class="field-group">
            <label for="workdir-input">Storage Folder (iCloud Drive or Local)</label>
            <input
              type="text"
              id="workdir-input"
              name="workdir"
              spellcheck="false"
              autocorrect="off"
              autocapitalize="none"
              placeholder="~icloud/LMU_Grading or ./sheets"
              required
            >
            <small id="resolved-path-display" class="mono" style="color: var(--color-text-muted);"></small>
          </div>
          <div class="button-row">
            <button type="submit" id="save-settings-btn">Save Folder &amp; Load Sheet</button>
            <button type="button" id="download-btn" class="primary">
              Download Assigned PDFs
            </button>
          </div>
        </form>
      </section>
    </div>

    <!-- Student Grading Table Card -->
    <section class="card" aria-labelledby="grading-section-title">
      <div class="section-bar">
        <div>
          <h2 id="grading-section-title" class="card-title">3. Assigned Submissions, Group Partners &amp; Points</h2>
          <p class="card-subtitle" style="margin-block-end: 0;">
            Sorted alphabetically by submitting student. Scrollable PDF preview (can be turned off), minimum-distance partner suggestion, and dual grading.
          </p>
        </div>
        <div class="button-row" style="margin-block-start: 0;">
          <label class="toggle-pill" for="toggle-pdf-preview" title="Toggle PDF preview on or off (stops background rendering when turned off)">
            <input type="checkbox" id="toggle-pdf-preview" checked>
            <span>Show PDF Preview</span>
          </label>
          <button type="button" id="refresh-table-btn">Refresh Files</button>
          <button type="button" id="detect-partners-btn">Detect Partners (PDF + OCR)</button>
          <button type="button" id="save-points-btn">Save Partners &amp; Points</button>
          <button type="button" id="open-upload-dialog-btn" class="primary">
            Review &amp; Upload to Moodle
          </button>
        </div>
      </div>

      <div class="safety-note" style="margin-block-end: 1rem;">
        <strong>Dual Group Grading &amp; Unreleased Guarantee:</strong> Uploading uploads the annotated PDF, enters the numerical points, and sets the grader (<span class="mono">Bewerter</span>) to your Moodle account for <strong>both</strong> students individually (<span class="mono">sendstudentnotifications = 0</span>, <span class="mono">Bewertungsstatus != Freigegeben</span>).
      </div>

      <datalist id="course-roster-datalist"></datalist>

      <div tabindex="0" role="region" aria-labelledby="grading-table-caption" class="table-wrapper">
        <table>
          <caption id="grading-table-caption">Assigned students (sorted alphabetically) and detected partners for current Übungsblatt</caption>
          <thead>
            <tr>
              <th scope="col">Submitting Student (A–Z)</th>
              <th scope="col">Partner (2nd Student in Ana 1 &amp; Scrollable PDF Preview)</th>
              <th scope="col">Unified PDF Filename</th>
              <th scope="col">Notability Status</th>
              <th scope="col">Numerical Points</th>
            </tr>
          </thead>
          <tbody id="students-tbody">
            <tr>
              <th scope="row" colspan="5">No submissions loaded for this sheet yet.</th>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </main>

  <!-- Native Modal Dialog with closedby="any" and Safari Light-Dismiss Fallback -->
  <dialog id="uploadConfirmDialog" closedby="any" aria-labelledby="confirmDialogTitle">
    <form method="dialog" id="confirm-dialog-form" class="form-stack">
      <h2 id="confirmDialogTitle" class="card-title" style="margin: 0;">
        Confirm Unreleased Upload to Moodle
      </h2>
      <p class="card-subtitle" style="margin: 0;">
        Verify the matched marked PDFs, group partners, and numerical grades below.
      </p>
      <div class="safety-note">
        ✓ Annotated PDF, numerical points, and your Moodle grader (<span class="mono">Bewerter</span>) are set for <strong>both</strong> students individually<br>
        ✓ Student notifications disabled (<span class="mono">sendstudentnotifications = 0</span>) &amp; kept <strong>unreleased</strong> in Moodle Marking Workflow.
      </div>
      <div id="dialog-summary-box" class="mono" style="max-block-size: 240px; overflow: auto; padding: 0.75rem; border: 1px solid var(--color-border); border-radius: var(--radius-control);"></div>
      <div class="button-row" style="justify-content: flex-end;">
        <button type="button" id="dialog-cancel-btn">Cancel</button>
        <button type="button" id="dialog-dryrun-btn">Run Dry-Run Check</button>
        <button type="button" id="dialog-confirm-upload-btn" class="primary">
          Confirm &amp; Upload (Keep Unreleased)
        </button>
      </div>
    </form>
  </dialog>

  <script>
    const CSRF_TOKEN = "__CSRF_TOKEN__";
    let currentState = { sheet: 1, students: [], course_roster: [], excluded_trainers: [], show_pdf_preview: true };
    let pdfPreviewEnabled = true;
    let previewRunId = 0;          // Cancellation token to immediately halt background PDF rendering
    const headerPreviewCache = {}; // key: `${sheet}:${filename}` -> dataURL
    const headerOcrCache = {};     // key: `${sheet}:${filename}` -> extracted text

    const dialog = document.getElementById("uploadConfirmDialog");
    // Mandatory fallback for browsers without native closedby support (Safari/WebKit)
    if (!("closedBy" in HTMLDialogElement.prototype)) {
      dialog.addEventListener("click", (event) => {
        if (event.target !== dialog) return;
        const rect = dialog.getBoundingClientRect();
        const isDialogContent = (
          rect.top <= event.clientY &&
          event.clientY <= rect.top + rect.height &&
          rect.left <= event.clientX &&
          event.clientX <= rect.left + rect.width
        );
        if (isDialogContent) return;
        dialog.close();
      });
    }

    document.getElementById("dialog-cancel-btn").addEventListener("click", () => {
      dialog.close();
    });

    const pwdInput = document.getElementById("current-password");
    const togglePwdBtn = document.getElementById("toggle-password-btn");
    togglePwdBtn.addEventListener("click", () => {
      const isHidden = pwdInput.type === "password";
      pwdInput.type = isHidden ? "text" : "password";
      togglePwdBtn.textContent = isHidden ? "Hide" : "Show";
      togglePwdBtn.setAttribute("aria-label", isHidden ? "Hide password" : "Show password");
    });

    function showBanner(message, isError = false) {
      const banner = document.getElementById("feedback-banner");
      const icon = document.getElementById("feedback-icon");
      const text = document.getElementById("feedback-message");
      banner.classList.remove("hidden", "ok", "err");
      banner.classList.add(isError ? "err" : "ok");
      icon.textContent = isError ? "❌" : "✓";
      text.textContent = message;
    }

    async function apiPost(path, payload, timeoutMs = 300000) {
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
      try {
        const resp = await fetch(path, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": CSRF_TOKEN
          },
          body: JSON.stringify(payload),
          signal: controller.signal
        });
        const data = await resp.json();
        if (!resp.ok || data.error) {
          throw new Error(data.error || `Request failed (${resp.status})`);
        }
        return data;
      } catch (err) {
        if (err.name === "AbortError") {
          throw new Error(
            `Request to ${path} timed out after ${Math.round(timeoutMs / 1000)}s while communicating with Moodle.`
          );
        }
        if (err instanceof TypeError) {
          throw new Error(
            "Connection to the local Python server in a-Shell was interrupted. Check the a-Shell terminal log."
          );
        }
        throw err;
      } finally {
        clearTimeout(timeoutId);
      }
    }

    function startProgressPolling(btn, defaultLabel) {
      return setInterval(async () => {
        try {
          const r = await fetch("/api/progress");
          if (r.ok) {
            const d = await r.json();
            if (d.progress) {
              btn.textContent = d.progress;
              showBanner(d.progress, false);
            }
          }
        } catch (_) {}
      }, 1000);
    }

    function collectPointsFromInputs() {
      const map = {};
      document.querySelectorAll(".points-input").forEach((inp) => {
        const fname = inp.dataset.filename;
        if (fname) {
          map[fname] = inp.value.trim();
        }
      });
      return map;
    }

    function collectPartnersFromInputs() {
      const map = {};
      document.querySelectorAll(".partner-input").forEach((inp) => {
        const fname = inp.dataset.filename;
        if (fname) {
          map[fname] = inp.value.trim();
        }
      });
      return map;
    }

    /**
     * Check whether a PDF.js page is a totally typed page (no raster images, no ink/note annotations,
     * and no handwritten vector strokes). Totally typed pages (like the assignment sheet) are excluded.
     */
    async function inspectPdfPageType(candidatePage) {
      let imageOpCount = 0;
      let strokeCoordScore = 0;
      let nonLinkAnnotCount = 0;
      let textItems = [];

      try {
        const textContent = await candidatePage.getTextContent();
        textItems = (textContent.items || []).filter((it) => it.str && it.str.trim());
      } catch (_) {}

      try {
        const annots = await candidatePage.getAnnotations();
        for (const a of annots || []) {
          if (a.subtype && a.subtype !== "Link") {
            nonLinkAnnotCount += 1;
          }
        }
      } catch (_) {}

      try {
        const opList = await candidatePage.getOperatorList();
        const OPS = window.pdfjsLib.OPS || {};
        const fnArray = opList.fnArray || [];
        const argsArray = opList.argsArray || [];
        for (let i = 0; i < fnArray.length; i++) {
          const fn = fnArray[i];
          if (
            fn === OPS.paintImageXObject ||
            fn === OPS.paintInlineImageXObject ||
            fn === OPS.paintImageMaskXObject ||
            fn === OPS.paintJpegXObject
          ) {
            imageOpCount += 1;
          } else if (fn === OPS.constructPath) {
            const args = argsArray[i];
            if (args && Array.isArray(args[1])) {
              strokeCoordScore += args[1].length;
            } else {
              strokeCoordScore += 4;
            }
          } else if (
            fn === OPS.curveTo ||
            fn === OPS.curveTo2 ||
            fn === OPS.curveTo3 ||
            fn === OPS.lineTo
          ) {
            strokeCoordScore += 2;
          }
        }
      } catch (_) {}

      const fullTypedStr = textItems.map((it) => it.str).join(" ").trim();
      const isTotallyTyped =
        imageOpCount === 0 &&
        nonLinkAnnotCount === 0 &&
        strokeCoordScore <= 50 &&
        fullTypedStr.length > 0;

      const lowerTyped = fullTypedStr.toLowerCase();
      let isAssignmentTextLayer = false;
      const excludedTrainers = Array.isArray(currentState.excluded_trainers)
        ? currentState.excluded_trainers
        : [];
      for (const trainer of excludedTrainers) {
        const parts = trainer.toLowerCase().split(/\s+/).filter((p) => p.length >= 3);
        if (parts.length >= 2 && parts.every((p) => lowerTyped.includes(p))) {
          isAssignmentTextLayer = true;
          break;
        }
      }

      return { isTotallyTyped, isAssignmentTextLayer, textItems };
    }

    /**
     * Render the first non-typed page of a student's PDF using PDF.js.
     * Returns a full-page scrollable preview image plus a cropped top-36% canvas for OCR.
     * Always destroys pdfDoc in finally so background PDF.js workers/connections never leak.
     */
    async function renderPdfTopHeader(sheet, filename, scale = 1.8) {
      if (typeof window.pdfjsLib === "undefined") {
        return null;
      }
      if (!window.pdfjsLib.GlobalWorkerOptions.workerSrc) {
        window.pdfjsLib.GlobalWorkerOptions.workerSrc =
          "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js";
      }
      const pdfUrl = `/api/pdf?sheet=${encodeURIComponent(sheet)}&filename=${encodeURIComponent(filename)}`;
      const loadingTask = window.pdfjsLib.getDocument({
        url: pdfUrl,
        disableRange: true,
        disableStream: true,
        disableAutoFetch: true
      });
      const pdfDoc = await loadingTask.promise;

      try {
        const maxPagesToCheck = Math.min(pdfDoc.numPages || 1, 5);
        let page = null;
        let pageInfo = null;
        for (let pNum = 1; pNum <= maxPagesToCheck; pNum++) {
          const candPage = await pdfDoc.getPage(pNum);
          const info = await inspectPdfPageType(candPage);
          if (info.isTotallyTyped) {
            continue;
          }
          page = candPage;
          pageInfo = info;
          break;
        }

        if (!page) {
          return { cropCanvas: null, previewDataUrl: null, pdfjsTopText: "", skippedTyped: true };
        }

        const viewport = page.getViewport({ scale });

        // Extract coordinate-sorted top-of-page embedded text from PDF.js unless it is the assignment sheet layer
        let pdfjsTopText = "";
        try {
          if (pageInfo && !pageInfo.isAssignmentTextLayer) {
            const pageH = page.view ? (page.view[3] - page.view[1]) : (viewport.height / scale);
            const topCutoff = pageH * 0.54;
            const items = [...(pageInfo.textItems || [])];
            items.sort((a, b) => {
              const ya = a.transform ? a.transform[5] : 0;
              const yb = b.transform ? b.transform[5] : 0;
              if (Math.abs(yb - ya) > 4) return yb - ya;
              const xa = a.transform ? a.transform[4] : 0;
              const xb = b.transform ? b.transform[4] : 0;
              return xa - xb;
            });
            const topItems = items.filter((it) => !it.transform || it.transform[5] >= topCutoff);
            pdfjsTopText = (topItems.length > 0 ? topItems : items.slice(0, 40))
              .map((it) => it.str)
              .join(" ");
          }
        } catch (_) {}

        const fullCanvas = document.createElement("canvas");
        fullCanvas.width = Math.floor(viewport.width);
        fullCanvas.height = Math.floor(viewport.height);
        const fullCtx = fullCanvas.getContext("2d");
        fullCtx.fillStyle = "#ffffff";
        fullCtx.fillRect(0, 0, fullCanvas.width, fullCanvas.height);
        await page.render({ canvasContext: fullCtx, viewport }).promise;

        // Full-page JPEG so the user can scroll through the entire page inside .header-preview-scroll
        const previewDataUrl = fullCanvas.toDataURL("image/jpeg", 0.78);
        headerPreviewCache[`${sheet}:${filename}`] = previewDataUrl;

        // Crop to the top 36% of the page where student names & 8-digit matriculation numbers sit for OCR
        const cropHeight = Math.max(180, Math.floor(fullCanvas.height * 0.36));
        const cropCanvas = document.createElement("canvas");
        cropCanvas.width = fullCanvas.width;
        cropCanvas.height = cropHeight;
        const cropCtx = cropCanvas.getContext("2d");
        cropCtx.drawImage(fullCanvas, 0, 0, fullCanvas.width, cropHeight, 0, 0, fullCanvas.width, cropHeight);

        // Preprocess cropCanvas in-place for high-contrast OCR (handles dark mode, colored ink, and grid paper)
        const imgData = cropCtx.getImageData(0, 0, cropCanvas.width, cropCanvas.height);
        const data = imgData.data;
        let lumSum = 0;
        const pixelCount = data.length / 4;
        for (let i = 0; i < data.length; i += 4) {
          lumSum += 0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2];
        }
        const avgLum = lumSum / Math.max(1, pixelCount);
        const invertDarkPaper = avgLum < 115;

        for (let i = 0; i < data.length; i += 4) {
          let r = data[i], g = data[i + 1], b = data[i + 2];
          if (invertDarkPaper) {
            r = 255 - r; g = 255 - g; b = 255 - b;
          }
          const minChan = Math.min(r, g, b);
          const lum = 0.299 * r + 0.587 * g + 0.114 * b;
          const val = Math.min(lum, minChan + 18);
          const stretched = val > 205 ? 255 : Math.max(0, Math.floor((val - 30) * (255 / 175)));
          data[i] = stretched;
          data[i + 1] = stretched;
          data[i + 2] = stretched;
        }
        cropCtx.putImageData(imgData, 0, 0);

        return { cropCanvas, previewDataUrl, pdfjsTopText };
      } finally {
        try {
          await pdfDoc.destroy();
        } catch (_) {}
      }
    }

    async function populateVisualHeaderPreviews(state, runId) {
      if (!pdfPreviewEnabled || runId !== previewRunId) return;
      if (typeof window.pdfjsLib === "undefined") return;
      const sheet = state.sheet;
      for (const st of state.students || []) {
        if (!pdfPreviewEnabled || runId !== previewRunId) return;
        if (st.state === "NO_PDF") continue;
        const key = `${sheet}:${st.filename}`;
        const wrapEl = document.getElementById(`preview-wrap-${st.filename}`);
        const imgEl = document.getElementById(`preview-img-${st.filename}`);
        if (!wrapEl || !imgEl) continue;
        if (headerPreviewCache[key]) {
          imgEl.src = headerPreviewCache[key];
          wrapEl.classList.remove("hidden");
          continue;
        }
        try {
          const res = await renderPdfTopHeader(sheet, st.filename, 1.5);
          if (!pdfPreviewEnabled || runId !== previewRunId) return;
          if (res && res.previewDataUrl) {
            imgEl.src = res.previewDataUrl;
            wrapEl.classList.remove("hidden");
          }
        } catch (_) {}
      }
    }

    /**
     * Run client-side PDF.js + Tesseract.js OCR on the top 36% of each student's PDF
     * and send the extracted text to /api/detect-partners for minimum-distance roster matching.
     */
    async function runFullPartnerDetection(sheet) {
      const btn = document.getElementById("detect-partners-btn");
      const origText = btn.textContent;
      btn.disabled = true;

      try {
        const studentsWithPdf = (currentState.students || []).filter((s) => s.state !== "NO_PDF");
        const ocrTexts = {};

        let worker = null;
        if (typeof window.Tesseract !== "undefined" && studentsWithPdf.length > 0) {
          btn.textContent = "Loading OCR engine...";
          showBanner("Initializing local WebKit OCR engine (deu+eng) for top-of-page name & Matrikelnummer scan...");
          try {
            worker = await window.Tesseract.createWorker("deu+eng");
          } catch (_) {
            try {
              worker = await window.Tesseract.createWorker("eng");
            } catch (_2) {
              worker = null;
            }
          }
        }

        for (let idx = 0; idx < studentsWithPdf.length; idx++) {
          const st = studentsWithPdf[idx];
          const cacheKey = `${sheet}:${st.filename}`;
          const statusMsg = `Scanning top of PDF (${idx + 1}/${studentsWithPdf.length}): ${st.fullname}...`;
          btn.textContent = `OCR ${idx + 1}/${studentsWithPdf.length}...`;
          showBanner(statusMsg);

          if (headerOcrCache[cacheKey]) {
            ocrTexts[st.filename] = headerOcrCache[cacheKey];
            continue;
          }

          try {
            const rendered = await renderPdfTopHeader(sheet, st.filename, 2.2);
            if (rendered) {
              if (pdfPreviewEnabled) {
                const wrapEl = document.getElementById(`preview-wrap-${st.filename}`);
                const imgEl = document.getElementById(`preview-img-${st.filename}`);
                if (wrapEl && imgEl && rendered.previewDataUrl) {
                  imgEl.src = rendered.previewDataUrl;
                  wrapEl.classList.remove("hidden");
                }
              }
              let combined = rendered.pdfjsTopText || "";
              if (worker && rendered.cropCanvas) {
                const { data } = await worker.recognize(rendered.cropCanvas);
                if (data && data.text) {
                  combined = `${data.text}\n${combined}`.trim();
                }
              }
              if (combined) {
                headerOcrCache[cacheKey] = combined;
                ocrTexts[st.filename] = combined;
              }
            }
          } catch (renderErr) {
            console.warn("Header OCR warning for", st.filename, renderErr);
          }
        }

        if (worker) {
          try {
            await worker.terminate();
          } catch (_) {}
        }

        btn.textContent = "Matching against Ana 1 roster...";
        const res = await apiPost("/api/detect-partners", { sheet, ocr_texts: ocrTexts });
        showBanner(
          `Matched ${res.matched_count} of ${studentsWithPdf.length} submission(s) against ${res.roster_count} students in the Ana 1 roster (minimum-distance suggestions ready for review).`
        );
        await loadState(sheet);
      } catch (err) {
        showBanner(err.message, true);
      } finally {
        btn.disabled = false;
        btn.textContent = origText;
      }
    }

    function renderState(state) {
      // Cancel any previous background preview rendering loop
      const myRunId = ++previewRunId;
      currentState = state;

      if (typeof state.show_pdf_preview === "boolean") {
        pdfPreviewEnabled = state.show_pdf_preview;
        const toggleEl = document.getElementById("toggle-pdf-preview");
        if (toggleEl) {
          toggleEl.checked = pdfPreviewEnabled;
        }
      }

      const badge = document.getElementById("auth-status-badge");
      const badgeText = document.getElementById("auth-status-text");
      if (state.logged_in) {
        badge.className = "badge ok";
        badgeText.textContent = `Signed In: ${state.fullname || state.username}`;
      } else {
        badge.className = "badge warn";
        badgeText.textContent = "Not Signed In";
      }

      const rosterCount = (state.course_roster || []).length;
      const rosterBadge = document.getElementById("roster-count-badge");
      if (rosterBadge) {
        rosterBadge.textContent = `👥 Ana 1 Roster: ${rosterCount} students`;
        rosterBadge.className = rosterCount > 20 ? "badge ok" : "badge warn";
      }

      if (state.username && !document.getElementById("lmu-username").value) {
        document.getElementById("lmu-username").value = state.username;
      }
      const markerInp = document.getElementById("marker-input");
      if (markerInp && !markerInp.value && (state.marker || state.fullname)) {
        markerInp.value = state.marker || state.fullname;
      }
      document.getElementById("sheet-number").value = state.sheet;
      document.getElementById("course-id").value = state.course_id;
      document.getElementById("workdir-input").value = state.workdir;
      document.getElementById("resolved-path-display").textContent =
        `Active path: ${state.resolved_workdir}/sheet_${state.sheet}`;

      // Populate course roster datalist for partner autocomplete
      const datalist = document.getElementById("course-roster-datalist");
      datalist.innerHTML = "";
      (state.course_roster || []).forEach((cs) => {
        const opt = document.createElement("option");
        opt.value = `${cs.fullname} (${cs.userid})`;
        datalist.appendChild(opt);
      });

      const tbody = document.getElementById("students-tbody");
      tbody.innerHTML = "";
      if (!state.students || state.students.length === 0) {
        const tr = document.createElement("tr");
        const th = document.createElement("th");
        th.scope = "row";
        th.colSpan = 5;
        th.textContent = "No downloaded submissions found for this sheet yet. Sign in and tap 'Download Assigned PDFs'.";
        tr.appendChild(th);
        tbody.appendChild(tr);
        return;
      }

      // Sort submissions alphabetically by submitting student name
      const sortedStudents = [...state.students].sort((a, b) =>
        (a.fullname || "").localeCompare(b.fullname || "", "de", { sensitivity: "base" })
      );
      currentState.students = sortedStudents;

      sortedStudents.forEach((st) => {
        const tr = document.createElement("tr");

        const thName = document.createElement("th");
        thName.scope = "row";
        thName.textContent = st.fullname;
        tr.appendChild(thName);

        const tdPartner = document.createElement("td");

        // Scrollable PDF page preview (only rendered when pdfPreviewEnabled is true)
        if (st.state !== "NO_PDF" && pdfPreviewEnabled) {
          const previewWrap = document.createElement("div");
          previewWrap.id = `preview-wrap-${st.filename}`;
          previewWrap.className = "header-preview-scroll hidden";
          previewWrap.title = "Scroll to inspect the page, or tap to expand/collapse preview size";

          const previewImg = document.createElement("img");
          previewImg.id = `preview-img-${st.filename}`;
          previewImg.className = "header-preview-img";
          previewImg.alt = `PDF Page Preview for ${st.fullname}`;

          const cachedUrl = headerPreviewCache[`${state.sheet}:${st.filename}`];
          if (cachedUrl) {
            previewImg.src = cachedUrl;
            previewWrap.classList.remove("hidden");
          }
          previewWrap.addEventListener("click", () => {
            previewWrap.classList.toggle("expanded");
          });
          previewWrap.appendChild(previewImg);
          tdPartner.appendChild(previewWrap);
        }

        const partnerInput = document.createElement("input");
        partnerInput.type = "text";
        partnerInput.className = "partner-input";
        partnerInput.setAttribute("list", "course-roster-datalist");
        partnerInput.placeholder = "Search Ana 1 student...";
        partnerInput.value = st.partner_display || "";
        partnerInput.dataset.filename = st.filename;
        partnerInput.setAttribute("aria-label", `Partner for ${st.fullname}`);
        tdPartner.appendChild(partnerInput);

        if (st.partner_match_source || (st.detected_matriculations && st.detected_matriculations.length > 0)) {
          const meta = document.createElement("span");
          const isReview = (st.partner_match_source || "").includes("review");
          meta.className = isReview ? "partner-meta review" : "partner-meta";
          const parts = [];
          if (st.partner_match_source) {
            parts.push(`${isReview ? "🔍" : "✓"} ${st.partner_match_source}`);
          }
          if (st.detected_matriculations && st.detected_matriculations.length > 0) {
            parts.push(`Matr.: ${st.detected_matriculations.join(", ")}`);
          }
          meta.textContent = parts.join(" • ");
          tdPartner.appendChild(meta);
        }

        // One-tap minimum-distance candidate chips for quick review/selection
        if (st.partner_candidates && st.partner_candidates.length > 0) {
          const chipsWrap = document.createElement("div");
          chipsWrap.className = "candidate-chips";
          st.partner_candidates.slice(0, 4).forEach((candStr, idx) => {
            const chip = document.createElement("button");
            chip.type = "button";
            chip.className = "chip-btn";
            const shortLabel = candStr.replace(/\s*\([^()]+\)$/, "");
            chip.textContent = idx === 0 ? `★ ${shortLabel}` : shortLabel;
            chip.title = `Select ${candStr} as group partner`;
            chip.addEventListener("click", () => {
              partnerInput.value = candStr;
            });
            chipsWrap.appendChild(chip);
          });
          const clearChip = document.createElement("button");
          clearChip.type = "button";
          clearChip.className = "chip-btn";
          clearChip.textContent = "✕ Alone";
          clearChip.title = "Clear partner (single submission)";
          clearChip.addEventListener("click", () => {
            partnerInput.value = "";
          });
          chipsWrap.appendChild(clearChip);
          tdPartner.appendChild(chipsWrap);
        }

        tr.appendChild(tdPartner);

        const tdFile = document.createElement("td");
        tdFile.className = "mono";
        tdFile.textContent = st.marked_file || st.filename;
        tr.appendChild(tdFile);

        const tdStatus = document.createElement("td");
        const statusBadge = document.createElement("span");
        if (st.state === "MARKED") {
          statusBadge.className = "badge ok";
          statusBadge.textContent = "✓ Marked PDF Ready";
        } else if (st.state === "DOWNLOADED") {
          statusBadge.className = "badge warn";
          statusBadge.textContent = "📄 In to_mark/ (Awaiting Export)";
        } else {
          statusBadge.className = "badge err";
          statusBadge.textContent = "⚠ No PDF Submitted";
        }
        tdStatus.appendChild(statusBadge);
        tr.appendChild(tdStatus);

        const tdPoints = document.createElement("td");
        const ptsInput = document.createElement("input");
        ptsInput.type = "text";
        ptsInput.inputMode = "decimal";
        ptsInput.className = "points-input";
        ptsInput.placeholder = "e.g. 14.5";
        ptsInput.value = st.points || "";
        ptsInput.dataset.filename = st.filename;
        ptsInput.setAttribute("aria-label", `Points for ${st.fullname}`);
        tdPoints.appendChild(ptsInput);
        tr.appendChild(tdPoints);

        tbody.appendChild(tr);
      });

      // Render scrollable PDF previews asynchronously only if enabled
      if (pdfPreviewEnabled) {
        setTimeout(() => populateVisualHeaderPreviews(state, myRunId), 60);
      }
    }

    async function loadState(sheetOverride = null) {
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 15000);
      try {
        const stateUrl =
          sheetOverride !== null && sheetOverride !== undefined
            ? `/api/state?sheet=${encodeURIComponent(sheetOverride)}`
            : "/api/state";
        const resp = await fetch(`${stateUrl}${stateUrl.includes("?") ? "&" : "?"}_ts=${Date.now()}`, {
          cache: "no-store",
          signal: controller.signal
        });
        const data = await resp.json();
        renderState(data);
        return data;
      } catch (err) {
        showBanner(
          "Could not reach local server in a-Shell. Keep a-Shell active via internalbrowser or Split View.",
          true
        );
        return null;
      } finally {
        clearTimeout(timeoutId);
      }
    }

    document.getElementById("toggle-pdf-preview").addEventListener("change", async (e) => {
      pdfPreviewEnabled = Boolean(e.target.checked);
      // Increment previewRunId so any in-flight background PDF rendering stops immediately
      previewRunId++;
      try {
        await apiPost("/api/settings", { show_pdf_preview: pdfPreviewEnabled });
      } catch (_) {}
      currentState.show_pdf_preview = pdfPreviewEnabled;
      renderState(currentState);
      showBanner(
        pdfPreviewEnabled
          ? "PDF preview enabled."
          : "PDF preview turned off (background rendering stopped)."
      );
    });

    document.getElementById("login-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = document.getElementById("login-submit-btn");
      const origText = btn.textContent;
      btn.disabled = true;
      btn.textContent = "Signing in...";
      try {
        const username = document.getElementById("lmu-username").value.trim();
        const password = document.getElementById("current-password").value;
        const data = await apiPost("/api/login", { username, password });
        document.getElementById("current-password").value = "";
        showBanner(`Authenticated as ${data.fullname || username}.`);
        const curSheet = Number(document.getElementById("sheet-number").value) || null;
        await loadState(curSheet);
      } catch (err) {
        showBanner(err.message, true);
      } finally {
        btn.disabled = false;
        btn.textContent = origText;
      }
    });

    document.getElementById("settings-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        const sheet = Number(document.getElementById("sheet-number").value) || 1;
        const course_id = Number(document.getElementById("course-id").value) || 48957;
        const workdir = document.getElementById("workdir-input").value.trim();
        const marker = document.getElementById("marker-input").value.trim();
        const username = document.getElementById("lmu-username").value.trim();
        await apiPost("/api/settings", {
          sheet,
          course_id,
          workdir,
          marker,
          username,
          show_pdf_preview: pdfPreviewEnabled
        });
        showBanner(`Updated active folder and loaded Übungsblatt ${sheet}.`);
        await loadState(sheet);
      } catch (err) {
        showBanner(err.message, true);
      }
    });

    document.getElementById("download-btn").addEventListener("click", async () => {
      const btn = document.getElementById("download-btn");
      const origText = btn.textContent;
      btn.disabled = true;
      btn.textContent = "Downloading...";
      const pollId = startProgressPolling(btn, "Downloading...");
      try {
        const sheet = Number(document.getElementById("sheet-number").value) || 1;
        const course_id = Number(document.getElementById("course-id").value) || 48957;
        const workdir = document.getElementById("workdir-input").value.trim();
        const marker = document.getElementById("marker-input").value.trim();
        const res = await apiPost("/api/download", { sheet, course_id, workdir, marker }, 300000);
        clearInterval(pollId);
        await loadState(sheet);
        await runFullPartnerDetection(sheet);
      } catch (err) {
        showBanner(err.message, true);
      } finally {
        clearInterval(pollId);
        btn.disabled = false;
        btn.textContent = origText;
      }
    });

    document.getElementById("refresh-table-btn").addEventListener("click", async () => {
      const btn = document.getElementById("refresh-table-btn");
      const origText = btn.textContent;
      btn.disabled = true;
      btn.textContent = "Refreshing...";
      // Stop any running background PDF preview loop before refreshing
      previewRunId++;
      try {
        const sheet = Number(document.getElementById("sheet-number").value) || currentState.sheet || 1;
        const data = await loadState(sheet);
        if (data) {
          showBanner(`Refreshed files and status for Übungsblatt ${data.sheet}.`);
        }
      } finally {
        btn.disabled = false;
        btn.textContent = origText;
      }
    });

    document.getElementById("detect-partners-btn").addEventListener("click", async () => {
      const sheet = Number(document.getElementById("sheet-number").value) || 1;
      await runFullPartnerDetection(sheet);
    });

    document.getElementById("save-points-btn").addEventListener("click", async () => {
      const btn = document.getElementById("save-points-btn");
      const origText = btn.textContent;
      btn.disabled = true;
      btn.textContent = "Saving...";
      try {
        const sheet = Number(document.getElementById("sheet-number").value) || currentState.sheet || 1;
        const points = collectPointsFromInputs();
        const partners = collectPartnersFromInputs();
        const res = await apiPost("/api/save-points", { sheet, points, partners }, 20000);
        if (res && res.state) {
          renderState(res.state);
        } else {
          await loadState(sheet);
        }
        showBanner(`Overwrote and saved partners and points for Übungsblatt ${sheet}.`);
      } catch (err) {
        showBanner(err.message, true);
      } finally {
        btn.disabled = false;
        btn.textContent = origText;
      }
    });

    document.getElementById("open-upload-dialog-btn").addEventListener("click", async () => {
      const sheet = Number(document.getElementById("sheet-number").value) || currentState.sheet || 1;
      const points = collectPointsFromInputs();
      const partners = collectPartnersFromInputs();
      const res = await apiPost("/api/save-points", { sheet, points, partners }, 20000);
      if (res && res.state) {
        renderState(res.state);
      } else {
        await loadState(sheet);
      }

      const ready = (currentState.students || []).filter((s) => s.state === "MARKED" || s.marked_file);
      const summaryBox = document.getElementById("dialog-summary-box");
      if (ready.length === 0) {
        summaryBox.textContent = "No marked PDFs found in marked/ (or to_mark/). Export your annotated PDFs from Notability first.";
      } else {
        const lines = [];
        ready.forEach((s) => {
          lines.push(`• ${s.fullname} (Submitter): PDF=${s.marked_file} | Points: ${s.points || "(none)"} | Grader: You`);
          if (s.partner_fullname) {
            lines.push(`  ↳ Partner (PDF, Points & Grader): ${s.partner_fullname} (userid=${s.partner_userid || "?"}) | Points: ${s.points || "(none)"} | Grader: You`);
          }
        });
        summaryBox.innerHTML = lines.join("<br>");
      }
      dialog.showModal();
    });

    async function triggerUpload(dryRun) {
      const btn = document.getElementById("dialog-confirm-upload-btn");
      btn.disabled = true;
      try {
        const sheet = Number(document.getElementById("sheet-number").value) || currentState.sheet || 1;
        const points = collectPointsFromInputs();
        const partners = collectPartnersFromInputs();
        await apiPost("/api/save-points", { sheet, points, partners }, 20000);
        const res = await apiPost("/api/upload", { sheet, dry_run: dryRun, points, partners });
        dialog.close();
        showBanner(res.message);
        await loadState(sheet);
      } catch (err) {
        showBanner(err.message, true);
      } finally {
        btn.disabled = false;
      }
    }

    document.getElementById("dialog-dryrun-btn").addEventListener("click", () => triggerUpload(true));
    document.getElementById("dialog-confirm-upload-btn").addEventListener("click", () => triggerUpload(false));

    loadState();
  </script>
</body>
</html>
"""


class GradingAppState:
    """In-memory state for the local loopback GUI server."""

    def __init__(self) -> None:
        self.csrf_token: str = secrets.token_hex(16)
        self.client: LMUMoodleClient = LMUMoodleClient()
        self.progress_message: str = ""
        self.client.progress_callback = self._set_progress
        self.lock = threading.Lock()
        self.file_lock = threading.Lock()

    def _set_progress(self, msg: str) -> None:
        self.progress_message = msg

    def build_sheet_state(self, sheet_number: Optional[int] = None) -> Dict[str, Any]:
        with self.file_lock:
            settings = load_user_settings()
            sheet = int(sheet_number or settings.get("last_sheet") or 1)
            workdir_str = str(settings.get("workdir") or "./sheets")
            resolved_workdir = resolve_storage_path(workdir_str)

            sheet_dir = resolved_workdir / f"sheet_{sheet}"
            to_mark_dir = sheet_dir / "to_mark"
            marked_dir = sheet_dir / "marked"
            manifest_path = sheet_dir / f"manifest_sheet_{sheet}.json"
            grades_csv_path = sheet_dir / f"grades_sheet_{sheet}.csv"

            students_out: List[Dict[str, Any]] = []
            roster_out: List[Dict[str, Any]] = []
            if manifest_path.exists():
                _, _, _, students_by_filename, course_roster = load_manifest_with_roster(
                    manifest_path
                )
                roster_out = [
                    {"userid": cs.userid, "fullname": cs.fullname} for cs in course_roster
                ]
                csv_points = read_grades_csv(grades_csv_path)
                matched = match_marked_pdfs(marked_dir, students_by_filename, csv_points)
                matched_by_fname = {m.record.filename: m for m in matched}

                sorted_items = sorted(
                    students_by_filename.items(),
                    key=lambda kv: ((kv[1].fullname or "").lower(), kv[0].lower()),
                )

                for fname, rec in sorted_items:
                    in_to_mark = (to_mark_dir / fname).exists()
                    marked_item = matched_by_fname.get(fname)
                    pts = (
                        csv_points.get(fname)
                        or rec.current_grade
                        or (marked_item.points if marked_item and marked_item.points else "")
                        or ""
                    )
                    state_label = (
                        "MARKED"
                        if marked_item
                        else ("DOWNLOADED" if in_to_mark else "NO_PDF")
                    )
                    partner_display = ""
                    if rec.partner_fullname and rec.partner_userid:
                        partner_display = f"{rec.partner_fullname} ({rec.partner_userid})"
                    elif rec.partner_fullname:
                        partner_display = rec.partner_fullname

                    students_out.append(
                        {
                            "userid": rec.userid,
                            "fullname": rec.fullname,
                            "filename": fname,
                            "marked_file": marked_item.pdf_path.name if marked_item else None,
                            "state": state_label,
                            "points": pts,
                            "partner_userid": rec.partner_userid or "",
                            "partner_fullname": rec.partner_fullname or "",
                            "partner_display": partner_display,
                            "partner_match_source": rec.partner_match_source or "",
                            "partner_candidates": rec.partner_candidates or [],
                            "detected_matriculations": rec.detected_matriculations or [],
                            "header_excerpt": rec.header_excerpt or "",
                        }
                    )

            return {
                "logged_in": bool(self.client.sesskey),
                "fullname": self.client.fullname or "",
                "userid": self.client.userid or "",
                "username": settings.get("username", ""),
                "marker": settings.get("marker", ""),
                "course_id": int(settings.get("course_id", DEFAULT_COURSE_ID)),
                "workdir": workdir_str,
                "resolved_workdir": str(resolved_workdir),
                "sheet": sheet,
                "show_pdf_preview": bool(settings.get("show_pdf_preview", True)),
                "students": students_out,
                "course_roster": roster_out,
                "excluded_trainers": sorted(get_excluded_partner_names()),
            }


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with address reuse and daemon worker threads for iPadOS a-Shell."""

    allow_reuse_address = True
    daemon_threads = True


def make_handler_class(app_state: GradingAppState):
    class GradingRequestHandler(BaseHTTPRequestHandler):
        timeout = 15

        def log_message(self, format: str, *args: Any) -> None:
            # Suppress noisy request logs
            return

        def _send_json(self, payload: Dict[str, Any], status: int = HTTPStatus.OK) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                self.close_connection = True

        def _read_json_body(self) -> Dict[str, Any]:
            token = self.headers.get("X-CSRF-Token", "")
            if not secrets.compare_digest(token, app_state.csrf_token):
                raise PermissionError("Invalid CSRF token.")
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            return json.loads(raw.decode("utf-8"))

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/":
                    page = INDEX_HTML_TEMPLATE.replace("__CSRF_TOKEN__", app_state.csrf_token)
                    body = page.encode("utf-8")
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Connection", "close")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    self.close_connection = True
                    return

                if parsed.path == "/api/progress":
                    self._send_json({"progress": app_state.progress_message})
                    return

                if parsed.path == "/api/state":
                    qs = parse_qs(parsed.query)
                    sheet_vals = qs.get("sheet")
                    sheet_num = int(sheet_vals[0]) if sheet_vals and sheet_vals[0].isdigit() else None
                    self._send_json(app_state.build_sheet_state(sheet_num))
                    return

                if parsed.path == "/api/pdf":
                    qs = parse_qs(parsed.query)
                    sheet_vals = qs.get("sheet")
                    fname_vals = qs.get("filename")
                    sheet_num = int(sheet_vals[0]) if sheet_vals and sheet_vals[0].isdigit() else 1
                    raw_fname = (fname_vals[0] if fname_vals else "").strip()
                    safe_fname = Path(raw_fname).name
                    if not safe_fname or safe_fname != raw_fname or not safe_fname.lower().endswith(".pdf"):
                        self._send_json({"error": "Invalid PDF filename"}, status=HTTPStatus.BAD_REQUEST)
                        return

                    settings = load_user_settings()
                    resolved_workdir = resolve_storage_path(str(settings.get("workdir") or "./sheets"))
                    sheet_dir = resolved_workdir / f"sheet_{sheet_num}"
                    pdf_path = sheet_dir / "to_mark" / safe_fname
                    if not pdf_path.exists():
                        pdf_path = sheet_dir / "marked" / safe_fname
                    if not pdf_path.exists():
                        self._send_json({"error": "PDF not found"}, status=HTTPStatus.NOT_FOUND)
                        return

                    pdf_bytes = pdf_path.read_bytes()
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "application/pdf")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Connection", "close")
                    self.send_header("Content-Length", str(len(pdf_bytes)))
                    self.end_headers()
                    self.wfile.write(pdf_bytes)
                    self.close_connection = True
                    return

                self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            print(f"[GUI] Handling POST {parsed.path} ...", flush=True)
            try:
                body = self._read_json_body()

                if parsed.path == "/api/settings":
                    with app_state.file_lock:
                        updates: Dict[str, Any] = {}
                        if "workdir" in body:
                            updates["workdir"] = str(body["workdir"]).strip()
                        if "course_id" in body:
                            updates["course_id"] = int(body["course_id"])
                        if "sheet" in body:
                            updates["last_sheet"] = int(body["sheet"])
                        if "marker" in body:
                            updates["marker"] = str(body["marker"]).strip()
                        if "username" in body and str(body["username"]).strip():
                            updates["username"] = str(body["username"]).strip()
                        if "show_pdf_preview" in body:
                            updates["show_pdf_preview"] = bool(body["show_pdf_preview"])
                        save_user_settings(updates)
                    self._send_json(app_state.build_sheet_state(updates.get("last_sheet")))
                    return

                if parsed.path == "/api/save-points":
                    sheet_num = int(body.get("sheet") or 1)
                    points_map: Dict[str, str] = body.get("points") if isinstance(body.get("points"), dict) else {}
                    partners_map: Optional[Dict[str, str]] = (
                        body.get("partners") if isinstance(body.get("partners"), dict) else None
                    )
                    with app_state.file_lock:
                        save_user_settings({"last_sheet": sheet_num})
                        settings = load_user_settings()
                        resolved_workdir = resolve_storage_path(str(settings.get("workdir") or "./sheets"))
                        sheet_dir = resolved_workdir / f"sheet_{sheet_num}"
                        manifest_path = sheet_dir / f"manifest_sheet_{sheet_num}.json"
                        grades_csv_path = sheet_dir / f"grades_sheet_{sheet_num}.csv"
                        if manifest_path.exists():
                            course_id, cmid, _, students_by_filename, course_roster = (
                                load_manifest_with_roster(manifest_path)
                            )
                            for fname, rec in students_by_filename.items():
                                # Always overwrite saved points for every submission on this sheet
                                if fname in points_map:
                                    rec.current_grade = str(points_map.get(fname) or "").strip()
                                elif points_map:
                                    rec.current_grade = ""

                                if partners_map is not None:
                                    cleaned_p = str(partners_map.get(fname) or "").strip()
                                    if not cleaned_p:
                                        rec.partner_userid = None
                                        rec.partner_fullname = None
                                        rec.partner_match_source = None
                                        forget_confirmed_partner(rec.userid)
                                    else:
                                        resolved_p = resolve_partner_by_query(
                                            cleaned_p, course_roster, exclude_userid=rec.userid
                                        )
                                        if resolved_p:
                                            rec.partner_userid = resolved_p.userid
                                            rec.partner_fullname = resolved_p.fullname
                                            rec.partner_match_source = "confirmed"
                                        else:
                                            rec.partner_userid = None
                                            rec.partner_fullname = cleaned_p
                                            rec.partner_match_source = "manual (unverified)"

                                        # Fast in-memory check against cached client rows if partner_userid is missing or temporary
                                        if rec.partner_fullname and (
                                            not rec.partner_userid or not str(rec.partner_userid).isdigit()
                                        ):
                                            from moodle_client import names_match

                                            for cached_r in app_state.client.last_all_rows:
                                                if str(cached_r.userid).isdigit() and names_match(
                                                    cached_r.fullname, rec.partner_fullname
                                                ):
                                                    rec.partner_userid = str(cached_r.userid)
                                                    rec.partner_fullname = cached_r.fullname
                                                    rec.partner_match_source = "confirmed"
                                                    if resolved_p:
                                                        resolved_p.userid = str(cached_r.userid)
                                                    break

                                        if rec.partner_userid and rec.partner_fullname:
                                            remember_confirmed_partner(rec)
                                        else:
                                            forget_confirmed_partner(rec.userid)

                            records_list = list(students_by_filename.values())
                            save_manifest(
                                manifest_path,
                                course_id,
                                cmid,
                                sheet_num,
                                records_list,
                                course_roster=course_roster,
                            )
                            update_grades_csv_entries(
                                grades_csv_path,
                                records_list,
                                {rec.filename: (rec.current_grade or "") for rec in records_list},
                            )
                    self._send_json({"ok": True, "state": app_state.build_sheet_state(sheet_num)})
                    return

                with app_state.lock:
                    if parsed.path == "/api/login":
                        username = str(body.get("username", "")).strip()
                        password = str(body.get("password", ""))
                        body.clear()
                        if username:
                            save_user_settings({"username": username})
                        app_state.client.login(username=username, password=password)
                        del password
                        print(
                            f"[GUI] Signed in as {app_state.client.fullname} (userid={app_state.client.userid})",
                            flush=True,
                        )
                        self._send_json(
                            {
                                "ok": True,
                                "fullname": app_state.client.fullname,
                                "userid": app_state.client.userid,
                            }
                        )
                        return

                    if parsed.path == "/api/download":
                        if not app_state.client.sesskey:
                            raise MoodleAuthError("Please sign in to LMU Moodle first.")
                        app_state.progress_message = "Starting download..."
                        sheet_num = int(body.get("sheet") or 1)
                        course_id = int(body.get("course_id") or DEFAULT_COURSE_ID)
                        marker_str = str(body.get("marker") or load_user_settings().get("marker") or "").strip()
                        workdir_str = str(body.get("workdir") or load_user_settings().get("workdir") or "./sheets")
                        save_user_settings(
                            {
                                "last_sheet": sheet_num,
                                "course_id": course_id,
                                "workdir": workdir_str,
                                "marker": marker_str,
                            }
                        )
                        resolved_workdir = resolve_storage_path(workdir_str)
                        sheet_dir = resolved_workdir / f"sheet_{sheet_num}"
                        to_mark_dir = sheet_dir / "to_mark"
                        marked_dir = sheet_dir / "marked"
                        manifest_path = sheet_dir / f"manifest_sheet_{sheet_num}.json"
                        grades_csv_path = sheet_dir / f"grades_sheet_{sheet_num}.csv"

                        cmid = app_state.client.find_assignment_cmid(
                            course_id=course_id, sheet_number=sheet_num
                        )
                        assigned_rows = app_state.client.fetch_assigned_submissions(
                            cmid=cmid, marker_query=marker_str or None
                        )
                        # Fetch the entire Ana 1 course roster across grading table, worksheet CSV, and participants page
                        all_course_rows = app_state.client.fetch_full_course_roster(
                            course_id=course_id, cmid=cmid
                        )
                        part_by_name = {
                            r.fullname.lower(): (r.participant_id, r.moodle_folder)
                            for r in all_course_rows
                            if r.participant_id
                        }
                        for row in assigned_rows:
                            if not row.participant_id and row.fullname.lower() in part_by_name:
                                row.participant_id, row.moodle_folder = part_by_name[
                                    row.fullname.lower()
                                ]

                        # Sort assigned submissions alphabetically by submitting student name
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

                        files_by_userid = app_state.client.download_submission_files_and_folders(
                            cmid, assigned_rows
                        )

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
                            rec = StudentSubmissionRecord(
                                userid=row.userid,
                                fullname=row.fullname,
                                firstname=firstname,
                                surname=surname,
                                filename=pdf_name,
                                sheet_number=sheet_num,
                                participant_id=row.participant_id,
                                moodle_folder=row.moodle_folder,
                                original_filenames=[f for f, _ in student_files],
                                current_grade=row.current_grade,
                            )
                            out_pdf_path: Optional[Path] = None
                            if student_files:
                                merged_bytes = merge_pdf_bytes_list(student_files)
                                out_pdf_path = to_mark_dir / pdf_name
                                out_pdf_path.write_bytes(merged_bytes)
                                downloaded_count += 1

                            app_state.progress_message = f"Matching group partner for {row.fullname}..."
                            matched_partner = detect_partner_for_record(
                                rec,
                                out_pdf_path,
                                course_roster,
                                marker_name=marker_str or app_state.client.fullname or "",
                            )
                            if matched_partner:
                                partners_matched += 1

                            records.append(rec)

                        with app_state.file_lock:
                            save_manifest(
                                manifest_path,
                                course_id,
                                cmid,
                                sheet_num,
                                records,
                                course_roster=course_roster,
                            )
                            sync_grades_csv(grades_csv_path, records, overwrite=True)
                        print(
                            f"[GUI] Downloaded {downloaded_count} PDF(s) for sheet {sheet_num}, "
                            f"roster={len(course_roster)}, matched {partners_matched} partner(s).",
                            flush=True,
                        )
                        self._send_json(
                            {
                                "ok": True,
                                "downloaded_count": downloaded_count,
                                "assigned_count": len(records),
                                "roster_count": len(course_roster),
                                "partners_matched": partners_matched,
                                "to_mark_dir": str(to_mark_dir),
                            }
                        )
                        return

                    if parsed.path == "/api/detect-partners":
                        sheet_num = int(body.get("sheet") or 1)
                        save_user_settings({"last_sheet": sheet_num})
                        ocr_texts: Dict[str, str] = body.get("ocr_texts") or {}
                        settings = load_user_settings()
                        marker_str = str(settings.get("marker") or app_state.client.fullname or "").strip()
                        resolved_workdir = resolve_storage_path(str(settings.get("workdir") or "./sheets"))
                        sheet_dir = resolved_workdir / f"sheet_{sheet_num}"
                        to_mark_dir = sheet_dir / "to_mark"
                        marked_dir = sheet_dir / "marked"
                        manifest_path = sheet_dir / f"manifest_sheet_{sheet_num}.json"
                        grades_csv_path = sheet_dir / f"grades_sheet_{sheet_num}.csv"

                        if not manifest_path.exists():
                            raise MoodleScrapingError(
                                f"No manifest found for sheet {sheet_num}. Download the sheet first."
                            )

                        course_id, cmid, _, students_by_filename, course_roster = (
                            load_manifest_with_roster(manifest_path)
                        )

                        # If the local roster only has the tutor's assigned students and we are signed in, fetch the full Ana 1 roster
                        if app_state.client.sesskey and len(course_roster) <= len(students_by_filename) + 5:
                            try:
                                full_rows = app_state.client.fetch_full_course_roster(
                                    course_id=course_id, cmid=cmid
                                )
                                if full_rows:
                                    course_roster = []
                                    for r in full_rows:
                                        f_n, s_n = split_fullname(r.fullname)
                                        course_roster.append(
                                            CourseStudent(
                                                userid=r.userid,
                                                fullname=r.fullname,
                                                firstname=f_n,
                                                surname=s_n,
                                                participant_id=r.participant_id,
                                                moodle_folder=r.moodle_folder,
                                                matriculation_number=r.matriculation_number,
                                            )
                                        )
                            except Exception as exc:
                                print(f"[GUI] Notice: Roster refresh during detect-partners skipped: {exc}", flush=True)

                        matched_count = 0
                        for fname, rec in students_by_filename.items():
                            pdf_path = to_mark_dir / fname
                            if not pdf_path.exists() and (marked_dir / fname).exists():
                                pdf_path = marked_dir / fname
                            extra_txt = str(ocr_texts.get(fname) or "")
                            partner = detect_partner_for_record(
                                rec,
                                pdf_path if pdf_path.exists() else None,
                                course_roster,
                                extra_top_text=extra_txt,
                                marker_name=marker_str,
                            )
                            if partner:
                                matched_count += 1

                        records_list = list(students_by_filename.values())
                        with app_state.file_lock:
                            save_manifest(
                                manifest_path,
                                course_id,
                                cmid,
                                sheet_num,
                                records_list,
                                course_roster=course_roster,
                            )
                            sync_grades_csv(grades_csv_path, records_list, overwrite=True)
                        self._send_json(
                            {
                                "ok": True,
                                "matched_count": matched_count,
                                "roster_count": len(course_roster),
                            }
                        )
                        return

                    if parsed.path == "/api/upload":
                        sheet_num = int(body.get("sheet") or 1)
                        save_user_settings({"last_sheet": sheet_num})
                        dry_run = bool(body.get("dry_run", False))
                        points_in: Optional[Dict[str, str]] = (
                            body.get("points") if isinstance(body.get("points"), dict) else None
                        )
                        partners_in: Optional[Dict[str, str]] = (
                            body.get("partners") if isinstance(body.get("partners"), dict) else None
                        )
                        settings = load_user_settings()
                        resolved_workdir = resolve_storage_path(str(settings.get("workdir") or "./sheets"))
                        sheet_dir = resolved_workdir / f"sheet_{sheet_num}"
                        to_mark_dir = sheet_dir / "to_mark"
                        marked_dir = sheet_dir / "marked"
                        manifest_path = sheet_dir / f"manifest_sheet_{sheet_num}.json"
                        grades_csv_path = sheet_dir / f"grades_sheet_{sheet_num}.csv"

                        if not manifest_path.exists():
                            raise MoodleScrapingError(
                                f"No manifest found for sheet {sheet_num}. Download the sheet first."
                            )

                        course_id, cmid, _, students_by_filename, course_roster = (
                            load_manifest_with_roster(manifest_path)
                        )
                        if points_in is not None or partners_in is not None:
                            with app_state.file_lock:
                                for fname, rec in students_by_filename.items():
                                    if points_in is not None:
                                        rec.current_grade = str(points_in.get(fname) or "").strip()
                                    if partners_in is not None:
                                        cleaned_p = str(partners_in.get(fname) or "").strip()
                                        if not cleaned_p:
                                            rec.partner_userid = None
                                            rec.partner_fullname = None
                                            rec.partner_match_source = None
                                            forget_confirmed_partner(rec.userid)
                                        else:
                                            resolved_p = resolve_partner_by_query(
                                                cleaned_p, course_roster, exclude_userid=rec.userid
                                            )
                                            if resolved_p:
                                                rec.partner_userid = resolved_p.userid
                                                rec.partner_fullname = resolved_p.fullname
                                                rec.partner_match_source = "confirmed"
                                            else:
                                                rec.partner_userid = None
                                                rec.partner_fullname = cleaned_p
                                                rec.partner_match_source = "manual (unverified)"
                                            if rec.partner_userid and rec.partner_fullname:
                                                remember_confirmed_partner(rec)
                                records_list = list(students_by_filename.values())
                                save_manifest(
                                    manifest_path,
                                    course_id,
                                    cmid,
                                    sheet_num,
                                    records_list,
                                    course_roster=course_roster,
                                )
                                update_grades_csv_entries(
                                    grades_csv_path,
                                    records_list,
                                    {rec.filename: (rec.current_grade or "") for rec in records_list},
                                )

                        csv_points = read_grades_csv(grades_csv_path)
                        primary_items = match_marked_pdfs(
                            marked_dir, students_by_filename, csv_points
                        )
                        if not primary_items and to_mark_dir.exists():
                            primary_items = match_marked_pdfs(
                                to_mark_dir, students_by_filename, csv_points
                            )
                        if not primary_items:
                            raise MoodleScrapingError(
                                f"No marked PDFs found in {marked_dir}."
                            )

                        # Expand items so BOTH submitting student and partner receive the PDF, points, and grader
                        all_graded_items = expand_marked_items_with_partners(
                            primary_items, course_roster
                        )
                        partner_extra_count = len(all_graded_items) - len(primary_items)

                        if not app_state.client.sesskey and not dry_run:
                            raise MoodleAuthError("Please sign in to LMU Moodle before uploading.")

                        # Resolve numeric Moodle userids for all submitting students and partners
                        if app_state.client.sesskey:
                            app_state.progress_message = "Resolving Moodle user IDs for students and partners..."
                            for g_it in all_graded_items:
                                if not str(g_it.record.userid).isdigit():
                                    resolved_row = app_state.client.resolve_student_userid_on_moodle(
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
                        skipped_items = []
                        if app_state.client.sesskey or app_state.client.last_all_rows:
                            all_graded_items, skipped_items = app_state.client.filter_items_graded_by_others(
                                cmid, all_graded_items
                            )
                        skipped_ids = {id(it) for it, _ in skipped_items}
                        primary_items = [it for it in primary_items if id(it) not in skipped_ids]
                        partner_extra_count = len(all_graded_items) - len(primary_items)
                        skipped_names = [it.record.fullname for it, _ in skipped_items]

                        if skipped_items:
                            skipped_str = ", ".join(
                                f"{it.record.fullname} ({reason})" for it, reason in skipped_items
                            )
                            print(
                                f"[GUI] Notice: Ignored {len(skipped_items)} student(s) already graded by someone else: {skipped_str}",
                                flush=True,
                            )

                        all_have_folder = all(
                            bool(it.record.moodle_folder or it.record.participant_id)
                            for it in primary_items
                        )
                        if dry_run:
                            skipped_notice = ""
                            if skipped_items:
                                skipped_notice = (
                                    f" Notice: {len(skipped_items)} student(s) ignored "
                                    f"(already graded by someone else): {', '.join(skipped_names)}."
                                )
                            if not all_graded_items:
                                msg = (
                                    f"Dry-run passed: all {len(skipped_items)} student(s) were ignored "
                                    f"because their submissions have already been graded by someone else.{skipped_notice}"
                                )
                                self._send_json(
                                    {
                                        "ok": True,
                                        "message": msg,
                                        "skipped_students": [
                                            {"name": it.record.fullname, "reason": r}
                                            for it, r in skipped_items
                                        ],
                                    }
                                )
                                return

                            if all_have_folder:
                                zbytes = build_moodle_feedback_zip(primary_items)
                                msg = (
                                    f"Dry-run passed: verified feedback archive ({len(zbytes)} bytes) "
                                    f"for {len(primary_items)} submitting student(s) and PDF/grades/grader for "
                                    f"{len(all_graded_items)} student(s) ({len(primary_items)} submitter(s) + {partner_extra_count} partner(s)). "
                                    f"Nothing sent to Moodle.{skipped_notice}"
                                )
                            else:
                                msg = (
                                    f"Dry-run passed: verified PDF upload for {len(primary_items)} submitting student(s) "
                                    f"and PDF/grades/grader for {len(all_graded_items)} student(s) "
                                    f"({len(primary_items)} submitter(s) + {partner_extra_count} partner(s)). Nothing sent to Moodle.{skipped_notice}"
                                )
                            self._send_json(
                                {
                                    "ok": True,
                                    "message": msg,
                                    "skipped_students": [
                                        {"name": it.record.fullname, "reason": r}
                                        for it, r in skipped_items
                                    ],
                                }
                            )
                            return

                        if not app_state.client.sesskey:
                            raise MoodleAuthError("Please sign in to LMU Moodle before uploading.")

                        if not all_graded_items:
                            msg = (
                                f"No submissions to upload. {len(skipped_items)} student(s) were ignored "
                                f"because their submissions have already been graded by someone else: "
                                f"{', '.join(skipped_names)}."
                            )
                            self._send_json(
                                {
                                    "ok": True,
                                    "message": msg,
                                    "skipped_students": [
                                        {"name": it.record.fullname, "reason": r}
                                        for it, r in skipped_items
                                    ],
                                }
                            )
                            return

                        # 2. Allocate logged-in tutor as Bewerter (allocatedmarker) for BOTH students first
                        #    so non-submitting partners are included in Moodle's assign::list_participants()
                        app_state.progress_message = "Setting you as Bewerter for submitting students and partners..."
                        app_state.client.allocate_marker_to_users(
                            cmid, [it.record.userid for it in all_graded_items]
                        )

                        app_state.progress_message = f"Uploading feedback PDFs for {len(all_graded_items)} student(s)..."
                        uploaded_pdfs = 0

                        # 3. Enrich participant IDs from worksheet now that partners are allocated to the tutor
                        if not all_have_folder:
                            part_map = app_state.client.fetch_participant_id_map(cmid)
                            if part_map:
                                for it in all_graded_items:
                                    if not it.record.participant_id:
                                        p_id = part_map.get(it.record.fullname.lower())
                                        if p_id:
                                            it.record.participant_id = p_id
                                            it.record.moodle_folder = f"{it.record.fullname}_{p_id}_assignsubmission_file_"
                                all_have_folder = all(
                                    bool(it.record.moodle_folder or it.record.participant_id)
                                    for it in primary_items
                                )

                        if all_have_folder:
                            try:
                                zip_items = [
                                    it
                                    for it in all_graded_items
                                    if it.record.moodle_folder or it.record.participant_id
                                ]
                                zbytes = build_moodle_feedback_zip(zip_items)
                                if app_state.client.upload_bulk_feedback_zip(cmid, zbytes):
                                    uploaded_pdfs = len(primary_items)
                            except Exception as zip_exc:
                                print(f"[GUI] Bulk ZIP upload fallback to form upload: {zip_exc}", flush=True)
                                uploaded_pdfs = 0

                        if uploaded_pdfs < len(primary_items):
                            count_batch, remaining = app_state.client.upload_batch_feedback_pdfs(
                                cmid, primary_items
                            )
                            uploaded_pdfs += count_batch
                            for item in remaining:
                                app_state.progress_message = f"Uploading feedback PDF for {item.record.fullname}..."
                                try:
                                    if app_state.client.upload_single_student_feedback_pdf(cmid, item):
                                        uploaded_pdfs += 1
                                except Exception as single_exc:
                                    print(
                                        f"[GUI] Notice: Feedback PDF upload skipped for {item.record.fullname}: {single_exc}",
                                        flush=True,
                                    )

                        # Also upload annotated PDF directly to each non-submitting partner so both students
                        # have the feedback file AND have their {assign_grades} row initialized in Moodle
                        partner_items = [
                            it
                            for it in all_graded_items
                            if it.record.partner_match_source == "partner_expansion"
                        ]
                        for p_item in partner_items:
                            app_state.progress_message = f"Uploading feedback PDF for partner {p_item.record.fullname}..."
                            try:
                                if app_state.client.upload_single_student_feedback_pdf(cmid, p_item):
                                    uploaded_pdfs += 1
                            except Exception as p_exc:
                                print(
                                    f"[GUI] Notice: Partner PDF upload skipped for {p_item.record.fullname}: {p_exc}",
                                    flush=True,
                                )

                        if uploaded_pdfs == 0 and len(primary_items) > 0:
                            raise MoodleScrapingError(
                                f"Failed to upload feedback PDFs to Moodle (0 of {len(primary_items)} succeeded). "
                                f"Please verify that Moodle feedback files are enabled and you have grading rights."
                            )

                        # 4. Submit numerical points and Bewerter for both submitting students and partners
                        app_state.progress_message = "Submitting points and grader to grading table for both students..."
                        graded_count = app_state.client.submit_grades_to_grading_table(
                            cmid, all_graded_items
                        )

                        # 5. Re-confirm Bewerter allocation for all submitting students and partners
                        app_state.client.allocate_marker_to_users(
                            cmid, [it.record.userid for it in all_graded_items]
                        )

                        # Persist any partner numeric userids resolved during upload back to manifest and CSV
                        manifest_updated = False
                        for g_it in all_graded_items:
                            if (
                                g_it.record.partner_match_source == "partner_expansion"
                                and str(g_it.record.userid).isdigit()
                            ):
                                orig_rec = students_by_filename.get(g_it.record.filename)
                                if orig_rec and orig_rec.partner_userid != str(g_it.record.userid):
                                    orig_rec.partner_userid = str(g_it.record.userid)
                                    remember_confirmed_partner(orig_rec)
                                    manifest_updated = True
                                for st in course_roster:
                                    if st.fullname.lower() == g_it.record.fullname.lower() and st.userid != str(g_it.record.userid):
                                        st.userid = str(g_it.record.userid)
                                        manifest_updated = True
                        if manifest_updated:
                            with app_state.file_lock:
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

                        app_state.progress_message = "Upload completed."
                        notice_suffix = ""
                        if skipped_items:
                            notice_suffix = (
                                f" Notice: Ignored {len(skipped_items)} student(s) already graded by someone else: "
                                f"{', '.join(skipped_names)}."
                            )
                        self._send_json(
                            {
                                "ok": True,
                                "message": (
                                    f"Uploaded {uploaded_pdfs} annotated PDF(s) and entered {graded_count} grade(s) "
                                    f"(with you as Bewerter) for {len(all_graded_items)} student(s) "
                                    f"({len(primary_items)} submitter(s) + {partner_extra_count} partner(s))."
                                    f"{notice_suffix}"
                                ),
                                "skipped_students": [
                                    {"name": it.record.fullname, "reason": r}
                                    for it, r in skipped_items
                                ],
                            }
                        )
                        return

                self._send_json({"error": "Unknown endpoint"}, status=HTTPStatus.NOT_FOUND)
            except PermissionError as exc:
                print(f"[GUI] Error on {parsed.path}: {exc}", flush=True)
                self._send_json({"error": str(exc)}, status=HTTPStatus.FORBIDDEN)
            except (SecurityError, MoodleAuthError, MoodleScrapingError, ValueError) as exc:
                print(f"[GUI] Error on {parsed.path}: {exc}", flush=True)
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                print(f"[GUI] Unexpected error on {parsed.path}: {exc}", flush=True)
                self._send_json({"error": f"Unexpected error: {exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    return GradingRequestHandler


def is_running_on_ipados() -> bool:
    """Detect whether Python is running inside a-Shell on iOS / iPadOS."""
    return (
        sys.platform == "ios"
        or "APP_SANDBOX_CONTAINER_ID" in os.environ
        or Path("/private/var/mobile").exists()
    )


def open_ipad_or_desktop_browser(url: str) -> None:
    """Open a-Shell's built-in internalbrowser on iPadOS, or default browser elsewhere."""
    if is_running_on_ipados():
        os.environ["BROWSER"] = "internalbrowser"
        os.system(f"internalbrowser {url}")
        return
    webbrowser.open(url)


def run_gui_server(port: int = 8765, open_browser: bool = True) -> None:
    """Start the local 127.0.0.1 web GUI server."""
    app_state = GradingAppState()
    handler_cls = make_handler_class(app_state)
    server = ReusableThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    actual_port = server.server_address[1]
    url = f"http://127.0.0.1:{actual_port}/"
    print(f"LMU Grading GUI running locally at {url}", flush=True)
    if open_browser:
        # Call on the main thread after socket bind/listen so a-Shell's thread-local ios_system session works
        open_ipad_or_desktop_browser(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down GUI server.", flush=True)
    finally:
        server.server_close()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Launch the local LMU Moodle & Notability Grading GUI."
    )
    parser.add_argument(
        "--port", type=int, default=8765, help="Localhost port (default: 8765)"
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not automatically launch internalbrowser / web browser",
    )
    args = parser.parse_args(argv)
    run_gui_server(port=args.port, open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    sys.exit(main())
