# -*- coding: utf-8 -*-
"""按书名首拼抓取 transchinese 三站 TXT 到本仓库。
- 只抓 .txt（不抓 epub）
- 跳过已存在且非空的文件，便于断点续传
- 分批 git commit/push，避免单次提交过大
"""
import os, re, sys, time, json, subprocess, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pypinyin import lazy_pinyin, Style

LETTER = os.environ.get("LETTER", "").strip().upper()
SITES = [s for s in os.environ.get("SITES", "").split(",") if s.strip()]
WORKERS = int(os.environ.get("WORKERS", "8"))
MIN_BYTES = int(os.environ.get("MIN_BYTES", "2048"))
MAX_BYTES = int(float(os.environ.get("MAX_MB", "90")) * 1024 * 1024)
COMMIT_EVERY = int(os.environ.get("COMMIT_EVERY", "150"))

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
    # 反复剥离开头的符号/序号，直到露出汉字或字母
    for _ in range(8):
        s = re.sub(r"^[^0-9A-Za-z\u4e00-\u9fff]+", "", s)
        s = re.sub(r"^[0-9\s._\-]+", "", s)
        if s and (("\u4e00" <= s[0] <= "\u9fff") or s[0].isalpha()):
            break
    if not s:
        return "#"
    ch = s[0]
    if "\u4e00" <= ch <= "\u9fff":
        py = lazy_pinyin(ch, style=Style.FIRST_LETTER)
        return (py[0].upper() if py and py[0] else "#")
    if ch.isalpha():
        return ch.upper()
    return "#"


def sanitize(name, maxlen=110):
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    return name[:maxlen] or "_"


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


def main():
    if not LETTER:
        print("未设置 LETTER"); sys.exit(1)
    print(f"===== 抓取首拼 [{LETTER}] =====", flush=True)

    # 汇总书目
    books = []
    for base in SITES:
        host = urllib.parse.urlparse(base).netloc
        for attempt in range(3):
            try:
                st, body = http_get(base + "/sitemap.xml", timeout=240)
                xml = body.decode("utf-8", "ignore")
                locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)
                pages = [l for l in locs if l.endswith("_page/")]
                print(f"  {host}: 详情页 {len(pages)}", flush=True)
                break
            except Exception as e:
                print(f"  {host} sitemap 失败({attempt+1}): {type(e).__name__}", flush=True)
                time.sleep(5)
                pages = []
        for p in pages:
            txt = p[:-6] + ".txt"
            segs = unquote_segs(urllib.parse.urlparse(txt).path)
            if len(segs) < 2:
                continue
            cat, name = segs[0], segs[-1]
            if not name.lower().endswith(".txt"):
                continue
            books.append((host, cat, name[:-4], txt))

    mine = [b for b in books if initial_of(b[2]) == LETTER]
    print(f"本仓库应入库 {len(mine)} 本（全站 {len(books)} 本）", flush=True)

    todo = []
    skip_exist = 0
    for host, cat, title, url in mine:
        rel = os.path.join(SITE_DIR.get(host, host.split(".")[0]), sanitize(cat), sanitize(title) + ".txt")
        full = os.path.join(".", rel)
        if os.path.exists(full) and os.path.getsize(full) >= MIN_BYTES:
            skip_exist += 1
            continue
        todo.append((rel, url))
    print(f"已存在跳过 {skip_exist} 本，待下载 {len(todo)} 本", flush=True)

    def work(item):
        rel, url = item
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
                if attempt == 2:
                    return ("bad", rel, st)
            except Exception as e:
                if attempt == 2:
                    return ("err", rel, f"{type(e).__name__}:{str(e)[:60]}")
            time.sleep(2 * (attempt + 1))
        return ("err", rel, "unknown")

    ok = n = 0
    total_bytes = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(work, t) for t in todo]
        for fu in as_completed(futs):
            kind, rel, info = fu.result()
            if kind == "ok":
                ok += 1
                total_bytes += info
            n += 1
            if n % COMMIT_EVERY == 0:
                commit_batch(f"chore: 同步小说 {n}/{len(todo)} 本")
                print(f"  进度 {n}/{len(todo)}，已入库 {ok} 本，{total_bytes/1048576:.0f} MB", flush=True)

    commit_batch(f"chore: 同步首拼 {LETTER} 小说（本次 {ok} 本）")
    print(f"\n完成：本次入库 {ok} 本 / 待下载 {len(todo)} 本，共 {total_bytes/1048576:.1f} MB，用时 {(time.time()-t0)/60:.1f} 分钟", flush=True)


if __name__ == "__main__":
    main()
