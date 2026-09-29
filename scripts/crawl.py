# -*- coding: utf-8 -*-
# 按书名首拼抓取 transchinese 三站 TXT（只抓 txt，不抓 epub）
# - 首拼采用词组上下文注音消歧多音字（重生=chong->C，长安=chang->C）
# - 书名规范化：去掉书名前的数字序号，保留书名号《》
# - 自动迁移：书名首拼不再属于本仓库的书删除，由对应字母仓库重新收录
# - 抓取完成后生成索引 README.md
import os, re, sys, time, subprocess, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pypinyin import lazy_pinyin, Style

LETTER = os.environ.get("LETTER", "").strip().upper()
SITES = [s for s in os.environ.get("SITES", "").split(",") if s.strip()]
WORKERS = int(os.environ.get("WORKERS", "8"))
MIN_BYTES = int(os.environ.get("MIN_BYTES", "2048"))
MAX_BYTES = int(float(os.environ.get("MAX_MB", "90")) * 1024 * 1024)
COMMIT_EVERY = int(os.environ.get("COMMIT_EVERY", "150"))
SKIP_DIRS = {".git", ".github", "scripts"}

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}
SITE_DIR = {
    "novel.transchinese.org": "novel",
    "xnovel.transchinese.org": "xnovel",
    "unovel.transchinese.org": "unovel",
}


def http_get(url, timeout=180):
    r = urllib.request.Request(url, headers=UA)
    r.add_header("Accept-Encoding", "identity")
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return resp.status, resp.read()


def unquote_segs(path):
    return [urllib.parse.unquote(s, encoding="utf-8", errors="replace") for s in path.strip("/").split("/")]


def initial_of(title):
    s = title.strip()
    for _ in range(8):
        s = re.sub(r"^[^0-9A-Za-z\u4e00-\u9fff]+", "", s)
        s = re.sub(r"^[0-9\s._\-]+", "", s)
        if s and (("\u4e00" <= s[0] <= "\u9fff") or s[0].isalpha()):
            break
    if not s:
        return "#"
    ch = s[0]
    if "\u4e00" <= ch <= "\u9fff":
        m = re.match(r"^[\u4e00-\u9fff]+", s)
        ctx = (m.group() if m else ch)[:8]
        py = lazy_pinyin(ctx, style=Style.FIRST_LETTER)
        return (py[0].upper() if py and py[0] else "#")
    if ch.isalpha():
        return ch.upper()
    return "#"


def clean_title(title):
    # 去掉书名前的数字序号（含括号序号），保留书名号《》
    s = title.strip()
    prev = None
    while prev != s and s:
        prev = s
        s2 = re.sub(r"^\d+[\s._\-\u00b7\u3001]*", "", s)
        if s2 != s:
            s = s2.strip()
            continue
        m = re.match(r"^(《)\s*\d+[\s._\-\u00b7\u3001]*", s)
        if m:
            s = s[:1] + s[m.end():]
            continue
        m = re.match(r"^([\(\[\uff08\u3010])\s*\d+\s*([\)\]\uff09\u3011])[\s._\-\u00b7\u3001]*", s)
        if m:
            s = s[m.end():]
            continue
    return s.strip() or title.strip()


def sanitize(name, maxlen=110):
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    return name[:maxlen] or "_"


def resolve_from_page(page_url):
    try:
        st, body = http_get(page_url, timeout=120)
        if st != 200:
            return None
        links = re.findall(r'href="([^"]+\.txt)"', body.decode("utf-8", "ignore"))
        if not links:
            return None
        return urllib.parse.urljoin(page_url, links[0])
    except Exception:
        return None


def git(*args):
    subprocess.run(["git", *args], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def commit_batch(note):
    git("add", "-A")
    r = subprocess.run(["git", "diff", "--staged", "--quiet"], check=False)
    if r.returncode == 0:
        return False
    git("config", "user.name", "github-actions[bot]")
    git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    subprocess.run(["git", "commit", "-m", note], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for attempt in range(5):
        r2 = subprocess.run(["git", "push"], check=False,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if r2.returncode == 0:
            return True
        time.sleep(5 * (attempt + 1))
    return False


def walk_txt():
    for root, dirs, files in os.walk("."):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in files:
            if fn.lower().endswith(".txt"):
                yield os.path.join(root, fn)


def reconcile():
    # 1) 首拼不属于本仓库的书删除（由对应仓库重新收录）
    # 2) 书名带序号的本仓库内改名（保留书名号），冲突时保留原名
    removed = renamed = 0
    for full in list(walk_txt()):
        fn = os.path.basename(full)
        title = fn[:-4]
        ct = clean_title(title)
        if initial_of(ct) != LETTER:
            git("rm", "-q", "--", full)
            removed += 1
            continue
        if ct == title:
            continue
        target = os.path.join(os.path.dirname(full), sanitize(ct) + ".txt")
        if os.path.exists(target):
            continue
        os.rename(full, target)
        renamed += 1
    if removed or renamed:
        commit_batch("chore: 整理书目（移出 %d 本，去序号改名 %d 本）" % (removed, renamed))
    return removed, renamed


def build_readme():
    entries = sorted(p.replace(os.sep, "/") for p in walk_txt())
    by_dir = {}
    for rel in entries:
        d = rel.rsplit("/", 1)[0]
        by_dir.setdefault(d, []).append(rel)
    lines = ["# %s 仓库索引" % LETTER, ""]
    lines.append("共收录 **%d** 本小说（txt 格式），按书名首拼 %s 归类。" % (len(entries), LETTER))
    lines.append("")
    lines.append("总索引见 [ACGN-Novel/U](https://github.com/ACGN-Novel/U)。")
    lines.append("")
    lines.append("| 站点/目录 | 数量 |")
    lines.append("| --- | --- |")
    for d in sorted(by_dir):
        lines.append("| %s | %d |" % (d, len(by_dir[d])))
    lines.append("")
    for d in sorted(by_dir):
        lines.append("## %s" % d)
        lines.append("")
        for rel in by_dir[d]:
            name = rel.rsplit("/", 1)[1][:-4]
            lines.append("- [%s](%s)" % (name, urllib.parse.quote(rel)))
        lines.append("")
    with open("README.md", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return len(entries)


def main():
    if not LETTER:
        print("未设置 LETTER"); sys.exit(1)
    print("===== 抓取首拼 [%s] =====" % LETTER, flush=True)

    removed, renamed = reconcile()
    print("已移出 %d 本，去序号改名 %d 本" % (removed, renamed), flush=True)

    books = []
    for base in SITES:
        host = urllib.parse.urlparse(base).netloc
        pages = []
        for attempt in range(3):
            try:
                st, body = http_get(base + "/sitemap.xml", timeout=240)
                locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body.decode("utf-8", "ignore"))
                pages = [l for l in locs if l.endswith("_page/")]
                break
            except Exception as e:
                print("  %s sitemap 失败(%d): %s" % (host, attempt + 1, type(e).__name__), flush=True)
                time.sleep(5)
        print("  %s: 详情页 %d" % (host, len(pages)), flush=True)
        for p in pages:
            txt = p[:-6] + ".txt"
            segs = unquote_segs(urllib.parse.urlparse(txt).path)
            if len(segs) < 2:
                continue
            cat, name = segs[0], segs[-1]
            if not name.lower().endswith(".txt"):
                continue
            books.append((host, cat, name[:-4], txt, p))

    mine = [b for b in books if initial_of(clean_title(b[2])) == LETTER]
    print("本仓库应入库 %d 本（全站 %d 本）" % (len(mine), len(books)), flush=True)

    todo, skip_exist = [], 0
    for host, cat, title, txt_url, page_url in mine:
        ct = clean_title(title)
        rel = os.path.join(SITE_DIR.get(host, host.split(".")[0]), sanitize(cat), sanitize(ct) + ".txt")
        full = os.path.join(".", rel)
        if os.path.exists(full) and os.path.getsize(full) >= MIN_BYTES:
            skip_exist += 1
            continue
        todo.append((rel, txt_url, page_url))
    print("已存在跳过 %d 本，待处理 %d 本" % (skip_exist, len(todo)), flush=True)

    def work(item):
        rel, url, page = item
        full = os.path.join(".", rel)
        for attempt in range(3):
            try:
                st, data = http_get(url, timeout=240)
                if st == 200 and MIN_BYTES <= len(data) <= MAX_BYTES:
                    os.makedirs(os.path.dirname(full), exist_ok=True)
                    tmp = full + ".part"
                    with open(tmp, "wb") as f:
                        f.write(data)
                    os.replace(tmp, full)
                    return ("ok", rel, len(data))
            except Exception:
                pass
            time.sleep(2 * (attempt + 1))
        real = resolve_from_page(page)
        if real:
            try:
                st2, data2 = http_get(real, timeout=240)
                if st2 == 200 and MIN_BYTES <= len(data2) <= MAX_BYTES:
                    os.makedirs(os.path.dirname(full), exist_ok=True)
                    tmp = full + ".part"
                    with open(tmp, "wb") as f:
                        f.write(data2)
                    os.replace(tmp, full)
                    return ("fixed", rel, len(data2))
            except Exception:
                pass
        return ("fail", rel, url)

    ok = fixed = n = 0
    total_bytes = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(work, t) for t in todo]
        for fu in as_completed(futs):
            kind, rel, info = fu.result()
            if kind in ("ok", "fixed"):
                if kind == "ok":
                    ok += 1
                else:
                    fixed += 1
                total_bytes += info
            n += 1
            if n % COMMIT_EVERY == 0:
                commit_batch("chore: 同步小说 %d/%d 本" % (n, len(todo)))
                print("  进度 %d/%d，已入库 %d 本，%.0f MB" % (n, len(todo), ok + fixed, total_bytes / 1048576), flush=True)

    commit_batch("chore: 同步首拼 %s 小说（直链 %d 本 + 补漏 %d 本）" % (LETTER, ok, fixed))

    total = build_readme()
    commit_batch("chore: 生成索引 README（共 %d 本）" % total)
    print("完成：移出 %d，改名 %d，直链 %d，补漏 %d，共 %.1f MB，索引 %d 本，用时 %.1f 分钟"
          % (removed, renamed, ok, fixed, total_bytes / 1048576, total, (time.time() - t0) / 60), flush=True)


if __name__ == "__main__":
    main()
