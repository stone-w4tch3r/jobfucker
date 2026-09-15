#!/usr/bin/env python3
# pyright: basic
# pyright: reportMissingModuleSource = none
# pyright: reportGeneralTypeIssues = none
# ruff: noqa
"""Headless repro of hh.ru test apply: GET page -> parse blob -> POST answers.

Reference example for docs/hh/tests.md §8, not production code.

Usage: python3 hhtest-repro-example.py <vacancy_id> <answer_text> [--dry]
Expects web cookies exported to /tmp/kilo/hhtests2/cookies-plain.json as
[{"name": ..., "value": ...}, ...] (incl. _xsrf, hhul, hhrole, hhtoken, __ddg*).
--dry stops after building the form (no application consumed).
One real application is consumed on success.
"""
import html
import json
import re
import sys
import time

import requests

VACANCY = sys.argv[1]
DRY = "--dry" in sys.argv
ANSWER = next((a for a in sys.argv[2:] if not a.startswith("--")), "Готов выполнить тестовое задание.")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36")
PAGE = f"https://hh.ru/applicant/vacancy_response?vacancyId={VACANCY}"
POST = "https://hh.ru/applicant/vacancy_response/popup"


def get_with_retry(s: requests.Session, url: str, tries: int = 4) -> requests.Response:
    # ddos-guard intermittently TLS-drops raw clients; retry with backoff is mandatory
    last = None
    for i in range(tries):
        try:
            return s.get(url, timeout=40)
        except requests.RequestException as e:
            last = e
            wait = 3 * (i + 1)
            print(f"GET retry {i+1}/{tries} after error: {type(e).__name__}; sleep {wait}s")
            time.sleep(wait)
    raise last


def main() -> int:
    jar = {}
    for c in json.load(open("/tmp/kilo/hhtests2/cookies-plain.json")):
        jar[c["name"]] = c["value"]
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9"})
    s.cookies.update(jar)

    # 1. GET apply page
    t0 = time.time()
    r = get_with_retry(s, PAGE)
    print(f"GET page -> {r.status_code} in {time.time()-t0:.1f}s, {len(r.text)} bytes")
    m = re.search(r'<template[^>]*id="HH-Lux-InitialState">(.*?)</template>', r.text, re.S)
    if not m:
        print("NO TEMPLATE — page shape changed or rate-limited")
        return 1
    # raw HTML keeps HTML-escaped JSON (&#34; for quotes); browser DOM auto-decodes
    state = json.loads(html.unescape(m.group(1)))
    test = state["vacancyTests"][VACANCY]
    st = state["applicantVacancyResponseStatuses"][VACANCY]
    resume_id = st["unusedResumeIds"][0]
    resume_hash = st["resumes"][resume_id]["_attributes"]["hash"]
    # jar may hold host+domain copies of _xsrf; page form value equals cookie value
    xsrf = next(c["value"] for c in json.load(open("/tmp/kilo/hhtests2/cookies-plain.json"))
                if c["name"] == "_xsrf")
    print(f"test: uidPk={test['uidPk']} guid={test['guid']} startTime={test['startTime']} "
          f"required={test['required']} tasks={len(test['tasks'])} resume_hash={resume_hash}")

    # 2. Build form: all tasks must be answered, else 400 {"error":"test-required"}
    form = {
        "_xsrf": xsrf,
        "uidPk": test["uidPk"],
        "guid": test["guid"],
        "startTime": test["startTime"],
        "testRequired": test["required"],
        "vacancy_id": VACANCY,
        "resume_hash": resume_hash,
        "ignore_postponed": "true",
        "incomplete": "false",
        "mark_applicant_visible_in_vacancy_country": "false",
        "country_ids": "[]",
        "letter": "",
        "lux": "true",
        "withoutTest": "no",
        "hhtmFromLabel": "",
        "hhtmSourceLabel": "",
    }
    for task in test["tasks"]:
        sols = task["candidateSolutions"]
        open_ = task.get("open") == "true"
        if sols and not open_:
            form[f"task_{task['id']}"] = sols[0]["id"]  # radio: pick first
        elif sols and open_:
            form[f"task_{task['id']}"] = "open"
            form[f"task_{task['id']}_text"] = ANSWER
        else:
            form[f"task_{task['id']}_text"] = ANSWER

    # 3. POST answers (no X-GIB headers, no X-Requested-With — check they are optional)
    if DRY:
        print("dry-run, form built:", list(form))
        return 0
    t0 = time.time()
    r = s.post(POST, files={k: (None, v) for k, v in form.items()},
               headers={"Referer": PAGE}, timeout=30)
    print(f"POST popup -> {r.status_code} in {time.time()-t0:.1f}s")
    print(r.text[:500])
    return 0 if r.status_code == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
