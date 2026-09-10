#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
orcid_sync.py — 把 ORCID 公开记录里的 works 同步成 academicpages (Jekyll) 的 _publications/*.md

只依赖 Python 标准库。生成的文件带 `orcid_putcode` 标记，
脚本只维护自己生成的条目，不会碰你手写的 publication 文件。

常用命令：
    python scripts/orcid_sync.py                       # ORCID 从 _config.yml 读取
    python scripts/orcid_sync.py --orcid 0009-0004-5701-7230
    python scripts/orcid_sync.py --dry-run             # 只看会做什么，不落盘
    python scripts/orcid_sync.py --prune               # 连带删除 ORCID 上已撤下的条目
    python scripts/orcid_sync.py --no-crossref         # 不查 Crossref（离线/被限流时用）
"""

import argparse
import json
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ORCID_API = "https://pub.orcid.org/v3.0"
CROSSREF_API = "https://api.crossref.org/works"
DEFAULT_UA = "academicpages-orcid-sync/1.0 (Jekyll academic homepage)"

# ORCID work type -> academicpages 的 publication_category
# _config.yml 里默认只定义了 books / manuscripts / conferences 三类，
# 落到未定义的类别页面上就不会显示，所以这里统一映射到已有的三类。
TYPE_MAP = {
    "journal-article": "manuscripts",
    "journal-issue": "manuscripts",
    "preprint": "manuscripts",
    "working-paper": "manuscripts",
    "report": "manuscripts",
    "dissertation": "manuscripts",
    "research-technique": "manuscripts",
    "conference-paper": "conferences",
    "conference-abstract": "conferences",
    "conference-poster": "conferences",
    "book": "books",
    "book-chapter": "books",
    "book-review": "books",
    "edited-book": "books",
    "monograph": "books",
    "reference-entry": "books",
}
DEFAULT_CATEGORY = "manuscripts"

BEGIN_MARK = "<!-- BEGIN ORCID SYNC -->"
END_MARK = "<!-- END ORCID SYNC -->"


# --------------------------------------------------------------------------- #
# 网络
# --------------------------------------------------------------------------- #
def fetch_json(url, accept="application/json", retries=3, timeout=30, verbose=False):
    """GET 一个 JSON，失败重试。返回 dict，彻底失败返回 None。"""
    for attempt in range(retries):
        try:
            req = Request(url, headers={"Accept": accept, "User-Agent": DEFAULT_UA})
            with urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            if e.code in (404, 400):
                return None  # 没有这条记录，不是故障
            if verbose:
                print(f"    ! HTTP {e.code} on {url}", file=sys.stderr)
        except (URLError, TimeoutError, json.JSONDecodeError) as e:
            if verbose:
                print(f"    ! {type(e).__name__} on {url}", file=sys.stderr)
        if attempt < retries - 1:
            time.sleep(2 * (attempt + 1))
    return None


def val(node):
    """ORCID 的 {"value": ...} 包装 -> 裸值。"""
    if isinstance(node, dict):
        return node.get("value")
    return node


# --------------------------------------------------------------------------- #
# ORCID 解析
# --------------------------------------------------------------------------- #
def fetch_orcid_works(orcid, verbose=False):
    """返回 ORCID 上的 work-summary 列表（已按日期新->旧排序）。"""
    data = fetch_json(f"{ORCID_API}/{orcid}/works", verbose=verbose)
    if not data:
        raise SystemExit(f"[x] 取不到 ORCID 数据：{orcid}（记录是私有的还是 iD 写错了？）")
    out = []
    for group in data.get("group", []):
        summaries = group.get("work-summary", [])
        if not summaries:
            continue
        # 同一成果可能重复导入多次（不同来源），取最新的一条
        summaries.sort(key=lambda w: val(w.get("created-date")) or 0)
        out.append(summaries[-1])
    out.sort(key=lambda w: sort_key(w), reverse=True)
    return out


def sort_key(w):
    y, m, d = parse_date(w)
    return (int(y), int(m or 1), int(d or 1))


def parse_date(w):
    pd = w.get("publication-date") or {}
    year = val(pd.get("year")) if pd.get("year") else None
    month = val(pd.get("month")) if pd.get("month") else None
    day = val(pd.get("day")) if pd.get("day") else None
    return year, month, day


def iso_date(w):
    y, m, d = parse_date(w)
    if not y:
        return date.today().isoformat(), ""
    m = int(m) if m and str(m).isdigit() else 1
    d = int(d) if d and str(d).isdigit() else 1
    m = min(max(m, 1), 12)
    d = min(max(d, 1), 28)  # 避免 2 月 30 这类脏数据
    return f"{int(y):04d}-{m:02d}-{d:02d}", str(int(y))


def external_ids(w):
    """external-ids -> {doi: ..., pmid: ...}"""
    ids = {}
    for e in (w.get("external-ids") or {}).get("external-id", []):
        t = (e.get("external-id-type") or "").lower()
        v = e.get("external-id-value")
        if t and v and t not in ids:
            ids[t] = v
    return ids


def work_contributors(orcid, putcode, verbose=False):
    """单独取一条 work 的详情拿作者列表（work-summary 里没有）。"""
    data = fetch_json(f"{ORCID_API}/{orcid}/work/{putcode}", verbose=verbose)
    if not data:
        return []
    names = []
    for c in ((data.get("contributors") or {}).get("contributor") or []):
        cn = c.get("credit-name")
        name = val(cn) if cn else None
        if not name:
            continue
        names.append({"given": "", "family": "", "literal": name})
    return names


def crossref_meta(doi, verbose=False):
    """用 DOI 去 Crossref 补作者/卷期页/摘要。失败返回 {}。"""
    if not doi:
        return {}
    data = fetch_json(f"{CROSSREF_API}/{doi}", verbose=verbose)
    if not data or "message" not in data:
        return {}
    m = data["message"]
    authors = []
    for a in m.get("author") or []:
        authors.append(
            {"given": a.get("given", ""), "family": a.get("family", ""), "literal": ""}
        )
    container = (m.get("container-title") or [None])[0]
    issued = (m.get("issued") or {}).get("date-parts") or [[None]]
    abstract = clean_abstract(m.get("abstract"))
    return {
        "authors": authors,
        "container": container,
        "volume": m.get("volume"),
        "issue": m.get("issue"),
        "page": m.get("page"),
        "publisher": m.get("publisher"),
        "year": issued[0][0] if issued and issued[0] else None,
        "abstract": abstract,
    }


def clean_abstract(raw):
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# --------------------------------------------------------------------------- #
# 文本处理
# --------------------------------------------------------------------------- #
def yaml_quote(s):
    """安全地放进 YAML 双引号标量。"""
    s = "" if s is None else str(s)
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    s = s.replace("\r", " ").replace("\n", " ").strip()
    return f'"{s}"'


def slugify(text, max_words=8):
    text = re.sub(r"[‘’“”]", "", text)
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    parts = [p for p in text.split("-") if p][:max_words]
    return "-".join(parts) or "publication"


def fmt_authors(authors, max_show=6):
    """Crossref 作者 -> 'W. Han, S. Richardson, ...'"""
    if not authors:
        return ""
    out = []
    for a in authors:
        given, family, literal = a.get("given", ""), a.get("family", ""), a.get("literal", "")
        if family:
            initials = "".join(p[0].upper() + "." for p in given.split() if p)
            out.append(f"{initials} {family}".strip())
        elif literal:
            out.append(literal)
    if not out:
        return ""
    if len(out) > max_show:
        out = out[:max_show] + ["et al."]
    return ", ".join(out)


def build_citation(title, authors, year, venue, volume, issue, page):
    bits = []
    if authors:
        bits.append(authors.rstrip(".") + ".")
    if year:
        bits.append(f"({year}).")
    bits.append(f"&quot;{title}.&quot;")
    if venue:
        bits.append(f"<i>{venue}</i>.")
    vol = ""
    if volume:
        vol = str(volume)
        if issue:
            vol += f"({issue})"
    if page:
        vol = f"{vol}, {page}" if vol else str(page)
    if vol:
        bits.append(vol + ".")
    return " ".join(bits)


# --------------------------------------------------------------------------- #
# 生成 markdown
# --------------------------------------------------------------------------- #
def build_entry(w, orcid, use_crossref=True, verbose=False):
    putcode = str(w.get("put-code"))
    title = (val(w.get("title", {}).get("title")) or "Untitled").strip()
    subtitle = val(w.get("title", {}).get("subtitle")) or ""
    wtype = w.get("type") or ""
    category = TYPE_MAP.get(wtype, DEFAULT_CATEGORY)
    journal = val(w.get("journal-title")) or ""
    pub_date, year = iso_date(w)
    ids = external_ids(w)
    doi = (ids.get("doi") or "").strip()
    url = val(w.get("url")) or (f"https://doi.org/{doi}" if doi else "")

    authors, volume, issue, page, abstract, publisher = [], "", "", "", "", ""
    if use_crossref and doi:
        cr = crossref_meta(doi, verbose=verbose)
        if cr:
            authors = cr.get("authors") or []
            journal = journal or (cr.get("container") or "")
            volume = cr.get("volume") or ""
            issue = cr.get("issue") or ""
            page = cr.get("page") or ""
            publisher = cr.get("publisher") or ""
            abstract = cr.get("abstract") or ""
            if cr.get("year"):
                year = str(cr["year"])
    if not authors:
        authors = work_contributors(orcid, putcode, verbose=verbose)

    authors_str = fmt_authors(authors)
    venue = journal or ""
    if not venue:
        # preprint 之类没有期刊名的，别把出版社名当成期刊，太怪
        venue = "Preprint" if wtype == "preprint" else (
            publisher or wtype.replace("-", " ").title() or "")
    citation = build_citation(title, authors_str, year, venue, volume, issue, page)
    slug = slugify(title)
    filename = f"{pub_date}-{slug}.md"
    permalink = f"/publication/{pub_date}-{slug}"

    return {
        "putcode": putcode,
        "title": title,
        "subtitle": subtitle,
        "category": category,
        "type": wtype,
        "venue": venue,
        "date": pub_date,
        "year": year,
        "doi": doi,
        "url": url,
        "authors": authors_str,
        "authors_full": authors,
        "citation": citation,
        "abstract": abstract,
        "filename": filename,
        "permalink": permalink,
        "publisher": publisher,
    }


def render_front_matter(entry, orcid, today):
    fm = [
        "---",
        f"title: {yaml_quote(entry['title'])}",
        "collection: publications",
        f"category: {entry['category']}",
        f"permalink: {entry['permalink']}",
        f"date: {entry['date']}",
        f"venue: {yaml_quote(entry['venue'])}",
    ]
    if entry["doi"]:
        fm.append(f"doi: {yaml_quote(entry['doi'])}")
        fm.append(f"paperurl: {yaml_quote('https://doi.org/' + entry['doi'])}")
    if entry["authors"]:
        fm.append(f"authors: {yaml_quote(entry['authors'])}")
    fm.append(f"citation: {yaml_quote(entry['citation'])}")
    fm.append(f"orcid: {yaml_quote(orcid)}")
    fm.append(f"orcid_putcode: {entry['putcode']}")
    fm.append(f"orcid_synced: {today}")
    fm.append("---")
    return "\n".join(fm)


def render_body(entry, orcid, today):
    body = [BEGIN_MARK]
    if entry["authors"]:
        body.append(f"**Authors:** {entry['authors']}")
        body.append("")
    if entry["venue"]:
        body.append(f"**Published in:** {entry['venue']}")
        body.append("")
    if entry["doi"]:
        body.append(f"**DOI:** [{entry['doi']}](https://doi.org/{entry['doi']})")
        body.append("")
    if entry["abstract"]:
        body.append("**Abstract:** " + entry["abstract"])
        body.append("")
    body.append(
        f"<sub>Auto-synced from [ORCID](https://orcid.org/{orcid}) on {today}. "
        f"Edit this record in ORCID; changes appear here within a day.</sub>"
    )
    body.append(END_MARK)
    return "\n".join(body)


def strip_front_matter(text):
    return re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S)


def merge_body(old_text, new_block):
    """保留用户在同步标记之外手写的内容，同步标记内的部分每次重建。"""
    rest = strip_front_matter(old_text).strip()
    if BEGIN_MARK in rest and END_MARK in rest:
        pre = rest.split(BEGIN_MARK)[0].strip()
        post = rest.split(END_MARK)[-1].strip()
    else:
        pre, post = rest, ""  # 没有标记：整段视为用户手写内容，原样保留在前
    out = (pre + "\n\n") if pre else ""
    out += new_block
    if post:
        out += "\n\n" + post
    return out


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def read_orcid_from_config(config_path):
    if not config_path or not Path(config_path).is_file():
        return None
    m = re.search(r"^\s*orcid\s*:\s*[\"']?(?:https?://orcid\.org/)?([0-9Xx-]{16,})",
                  Path(config_path).read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else None


def read_categories(config_path):
    """读 _config.yml 里 publication_category 定义了哪些分组。
    如果你自己加了 preprints / datasets 之类的分组，脚本会自动用上。"""
    cats = set()
    p = Path(config_path)
    if not p.is_file():
        return cats
    in_block = False
    for line in p.read_text(encoding="utf-8").splitlines():
        if re.match(r"^publication_category\s*:", line):
            in_block = True
            continue
        if in_block:
            m = re.match(r"^  ([A-Za-z0-9_-]+)\s*:\s*(#.*)?$", line)
            if m:
                cats.add(m.group(1))
            elif line.strip() and not line.startswith(" "):
                break
    return cats


def index_existing(outdir):
    """putcode -> Path，只索引脚本自己生成过的文件。"""
    idx = {}
    if not outdir.is_dir():
        return idx
    for p in sorted(outdir.glob("*.md")):
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        m = re.search(r"^orcid_putcode:\s*(\d+)", text, re.M)
        if m:
            idx[m.group(1)] = p
    return idx


def main():
    here = Path(__file__).resolve().parent
    root = here.parent  # 仓库根目录
    ap = argparse.ArgumentParser(description="Sync ORCID works into Jekyll _publications/")
    ap.add_argument("--orcid", default=None, help="ORCID iD，如 0009-0004-5701-7230")
    ap.add_argument("--out", default=str(root / "_publications"), help="输出目录")
    ap.add_argument("--config", default=str(root / "_config.yml"), help="从 _config.yml 读 ORCID")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--prune", action="store_true", help="删除 ORCID 上已不存在的同步条目")
    ap.add_argument("--no-crossref", action="store_true", help="不查 Crossref")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    orcid = (args.orcid or read_orcid_from_config(args.config) or "").strip()
    orcid = re.sub(r"^https?://orcid\.org/", "", orcid).strip("/")
    if not re.fullmatch(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]", orcid, re.I):
        raise SystemExit("[x] 没拿到合法 ORCID iD，用 --orcid 指定或在 _config.yml 里配好 author.orcid")

    outdir = Path(args.out)
    today = date.today().isoformat()

    # 站点自定义了额外分组（如 preprints）就用它，否则退回 manuscripts
    cats = read_categories(args.config)
    for extra in ("preprint", "working-paper"):
        TYPE_MAP[extra] = "preprints" if "preprints" in cats else "manuscripts"

    print(f"[·] ORCID {orcid} -> {outdir}")

    works = fetch_orcid_works(orcid, verbose=args.verbose)
    print(f"[·] ORCID 上共 {len(works)} 条成果")

    idx = index_existing(outdir)
    added = updated = unchanged = 0
    seen = set()

    for w in works:
        entry = build_entry(w, orcid, use_crossref=not args.no_crossref, verbose=args.verbose)
        seen.add(entry["putcode"])
        fm = render_front_matter(entry, orcid, today)
        body = render_body(entry, orcid, today)

        target = idx.get(entry["putcode"])
        if target is None:
            target = outdir / entry["filename"]
            n = 1
            while target.exists():  # 同名但不同 putcode，另起一个名字
                n += 1
                target = outdir / entry["filename"].replace(".md", f"-{n}.md")
            status = "NEW"
        else:
            body = merge_body(target.read_text(encoding="utf-8"), body)
            status = "UPD"

        new_text = fm + "\n\n" + body + "\n"
        if status == "UPD" and new_text.strip() == target.read_text(encoding="utf-8").strip():
            unchanged += 1
            print(f"    = {target.name}")
            continue

        if args.dry_run:
            print(f"    [{status}] {target.name}  <- {entry['title'][:60]}")
        else:
            outdir.mkdir(parents=True, exist_ok=True)
            target.write_text(new_text, encoding="utf-8")
            print(f"    [{status}] {target.name}  <- {entry['title'][:60]}")
        if status == "NEW":
            added += 1
        else:
            updated += 1

    pruned = 0
    if args.prune:
        for pcode, p in idx.items():
            if pcode not in seen:
                if args.dry_run:
                    print(f"    [DEL] {p.name}")
                else:
                    p.unlink()
                    print(f"    [DEL] {p.name}")
                pruned += 1

    print(f"[√] 新增 {added} / 更新 {updated} / 未变 {unchanged} / 删除 {pruned}")
    if args.dry_run:
        print("    (dry-run，没有写文件)")


if __name__ == "__main__":
    main()
