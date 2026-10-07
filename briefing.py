#!/usr/bin/env python3
"""에너지 시장 일일 브리핑 -> 텔레그램 발송.

평일(공휴일 제외) 08:30 KST 에 SMP / 발전소 가동현황(TMS) / 시장지표 / 뉴스를 요약해 발송한다.
데이터는 3개 사이트 저장소(Energy-Index, KPX-Daily-Report, TMS)가 저장하는 파일을 읽는다.

사용 예
  python briefing.py --dry-run --no-wait              # 발송 없이 문구만 출력
  python briefing.py --dry-run --no-wait --local DIR  # 로컬에 clone 한 저장소로 시험
  python briefing.py                                   # 실제 발송(08:30 까지 대기)
환경변수: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (쉼표로 여러 개 가능)
"""
import argparse
import csv
import html
import io
import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
HERE = os.path.dirname(os.path.abspath(__file__))
WEEKDAY = "월화수목금토일"
UNIT_FIX = {"$/bbl": "USD/bbl"}


# ------------------------------------------------------------------ 공통
def md(d):
    return f"{d.month}.{d.day}"


def md_from_str(s):
    """'2026-10-06' 또는 '20261006' -> '10.6'"""
    s = str(s).replace("-", "")
    return f"{int(s[4:6])}.{int(s[6:8])}"


def arrow(diff, nd):
    if diff is None:
        return ""
    if abs(diff) < 0.5 * 10 ** (-nd):
        return "-"
    return ("▲" if diff > 0 else "▼") + f"{abs(diff):,.{nd}f}"


def esc(s):
    return html.escape(str(s), quote=False)


def fetch_text(key, cfg, local, **fmt):
    if local:
        path = os.path.join(local, cfg["local_paths"][key].format(**fmt))
        with open(path, encoding="utf-8-sig") as f:
            return f.read()
    url = cfg["sources"][key].format(**fmt)
    last = None
    for i in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "briefing-bot/1.0", "Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read().decode("utf-8-sig")
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 404:
                break
            time.sleep(3 * (i + 1))
        except Exception as e:  # 네트워크 오류 등
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"{key} 수신 실패({type(last).__name__})")


def fetch_json(key, cfg, local, **fmt):
    return json.loads(fetch_text(key, cfg, local, **fmt))


# ------------------------------------------------------------------ 휴일 판정
def is_off_day(d, cfg, local):
    """주말·공휴일이면 사유 문자열, 평일이면 None."""
    if d.weekday() >= 5:
        return f"주말({WEEKDAY[d.weekday()]})"
    key = d.strftime("%Y%m%d")
    names = {}
    try:
        names.update(fetch_json("holidays", cfg, local).get("holidays", {}))  # 사이트가 관리하는 공휴일표
    except Exception as e:
        print(f"[경고] 사이트 공휴일표 수신 실패: {e}", file=sys.stderr)
    try:
        import holidays as _h  # 선택: 법정·임시 공휴일 보완
        for dt, nm in _h.KR(years=d.year).items():
            names.setdefault(dt.strftime("%Y%m%d"), nm)
    except ImportError:
        pass
    for x in cfg.get("extra_holidays", []):
        names[str(x).replace("-", "")] = "지정 휴일"
    if key in names:
        return f"공휴일({names[key]})"
    return None


# ------------------------------------------------------------------ 섹션: SMP
def smp_stats(day):
    if not day:
        return None
    vals = day.get("smp") or []
    if len(vals) != 24 or any(v is None for v in vals):
        return None
    avg = day.get("wavg")
    if avg is None:
        avg = sum(vals) / 24
    hi, lo = max(vals), min(vals)
    return {"avg": avg, "max": hi, "maxh": vals.index(hi) + 1, "min": lo, "minh": vals.index(lo) + 1}


def smp_line(label, d, st, prev):
    head = f" - {label}({md(d)})"
    if st is None:
        return f"{head}: 미수신·미공표"
    s = f"{head}: 평균 {st['avg']:,.2f}원/kWh"
    if prev:
        s += f" (전일 대비 {arrow(st['avg'] - prev['avg'], 2)})"
    s += f", 최고 {st['max']:,.2f}({st['maxh']}시) / 최저 {st['min']:,.2f}({st['minh']}시)"
    return s


def sec_smp(ctx):
    days = fetch_json("smp", ctx.cfg, ctx.local)["days"]
    d0, d1, d2 = ctx.today, ctx.today - timedelta(days=1), ctx.today - timedelta(days=2)
    st = {d: smp_stats(days.get(d.strftime("%Y%m%d"))) for d in (d0, d1, d2)}
    return [smp_line("전일실적", d1, st[d1], st[d2]), smp_line("당일실적", d0, st[d0], st[d1])]


# ------------------------------------------------------------------ 섹션: TMS
def sec_tms(ctx):
    latest = fetch_json("tms_latest", ctx.cfg, ctx.local)
    items = latest.get("items", [])
    yday = ctx.today - timedelta(days=1)
    rows, rep_err = [], None
    try:
        rows = list(csv.DictReader(io.StringIO(fetch_text("tms_report", ctx.cfg, ctx.local, date=yday.isoformat()))))
    except Exception as e:
        rep_err = str(e)
    out = []
    for fac in ctx.cfg["facilities"]:
        # 전일 가동시간
        mine = [r for r in rows if r.get("사업장") == fac["match"]]
        if mine:
            r = mine[0]
            part = f"{float(r['가동시간(h)']):.1f}시간 가동"
            und = int(r.get("판정불가슬롯") or 0)
            if und > 0:
                part += f"(판정불가 {und * 0.5:.1f}시간 제외)"
            prev_txt = f"전일({md(yday)}) {part}"
        else:
            prev_txt = f"전일({md(yday)}) 가동시간 미수신"
        # 현재 상태
        cur = [i for i in items if i.get("facility") == fac["match"]]
        if cur:
            c = cur[0]
            state = {1: "가동중", 0: "정지중"}.get(c.get("running"), "판정불가")
            try:
                dt = datetime.fromisoformat(c["slot"]).astimezone(KST)
                when = dt.strftime("%H:%M") if dt.date() == ctx.today else f"{md(dt)} {dt.strftime('%H:%M')}"
                late = " ※수집 지연" if ctx.now - dt > timedelta(hours=3) else ""
                cur_txt = f"현재 {state} ({when} 기준{late})"
            except Exception:
                cur_txt = f"현재 {state}"
        else:
            cur_txt = "현재 상태 미수신"
        out.append(f" - {fac['label']}: {prev_txt} / {cur_txt}")
    if rep_err and not rows:
        ctx.warn(f"TMS 전일 리포트: {rep_err}")
    return out


# ------------------------------------------------------------------ 섹션: 시장지표
def sec_indicators(ctx):
    items = fetch_json("indicators", ctx.cfg, ctx.local).get("items", {})
    out = []
    for key in ctx.cfg["indicator_items"]:
        it = items.get(key)
        if not it or it.get("value") is None:
            out.append(f" - {key}: 미수신")
            continue
        won = it.get("unit", "").startswith("원")
        nd = 0 if won else 2
        val = f"{it['value']:,.{nd}f}"
        # 텔레그램이 '$/bbl' 의 '/bbl' 을 봇 명령어(링크)로 인식하므로 단위를 바꿔 표시
        unit = UNIT_FIX.get(it["unit"], it["unit"])
        meta = []
        if it.get("prev_value") is not None:
            meta.append(f"직전 대비 {arrow(it['value'] - it['prev_value'], nd)}")
        if it.get("date"):
            meta.append(md_from_str(it["date"]))
        out.append(f" - {it['name']} {val} {unit}" + (f" ({', '.join(meta)})" if meta else ""))
    return out


# ------------------------------------------------------------------ 섹션: 뉴스
def sec_news(ctx):
    data = fetch_json("news", ctx.cfg, ctx.local)
    out = []
    for n, it in enumerate(data.get("items", [])[: ctx.cfg["news_count"]], 1):
        link = html.escape(it["link"], quote=True)
        out.append(f' {n}. <a href="{link}">{esc(it["title"])}</a> ({esc(it["source"])})')
    if not out:
        out.append(" - 수집된 뉴스 없음")
    try:
        upd = datetime.strptime(data["updated"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
        if ctx.now - upd > timedelta(hours=36):
            out.append(f" ※ 뉴스 갱신 지연(최종 수집 {md(upd)} {upd.strftime('%H:%M')})")
    except Exception:
        pass
    return out


# ------------------------------------------------------------------ 조립·발송
class Ctx:
    def __init__(self, cfg, local, now):
        self.cfg, self.local, self.now = cfg, local, now
        self.today = now.date()
        self.errors = []

    def warn(self, msg):
        self.errors.append(msg)
        print(f"::warning::{msg}")


def safe(ctx, title, fn):
    try:
        lines = fn(ctx)
    except Exception as e:
        traceback.print_exc()
        ctx.warn(f"{title}: {e}")
        lines = [f" - 자료 수신 실패({type(e).__name__})"]
    return [f"<b>■ {title}</b>"] + lines + [""]


def build_message(ctx):
    d = ctx.today
    parts = [f"<b>[에너지 시장 일일 브리핑]</b> {md(d)}({WEEKDAY[d.weekday()]}) {ctx.cfg['send_time']}", ""]
    parts += safe(ctx, "계통한계가격(육지)", sec_smp)
    parts += safe(ctx, "발전소 가동현황 (TMS 기준)", sec_tms)
    if ctx.cfg.get("show_indicators", False):  # 시장지표는 현재 미표시(config.json 의 show_indicators 로 복원)
        parts += safe(ctx, "시장지표", sec_indicators)
    parts += safe(ctx, f"주요뉴스 {ctx.cfg['news_count']}건", sec_news)
    links = " · ".join(f'<a href="{html.escape(x["url"], quote=True)}">{esc(x["label"])}</a>' for x in ctx.cfg["links"])
    parts += ["<b>■ 바로가기</b>", f" {links}", "", "※ SMP 최고·최저 시각은 거래시간(1~24시) 기준"]
    text = "\n".join(parts)
    return text[:4000]


def _post(token, params):
    """sendMessage 1회 호출. (성공여부, 텔레그램 응답 dict 또는 오류 설명) 반환."""
    body = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return True, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            info = json.loads(e.read())
        except Exception:
            info = {}
        info["_code"] = e.code
        return False, info
    except Exception as e:
        return False, {"_code": 0, "description": type(e).__name__}


def send_one(token, chat, text):
    """한 대화방으로 발송. 성공 시 None, 실패 시 사유 문자열 반환."""
    params = {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}
    last = ""
    for i in range(4):
        ok, res = _post(token, params)
        if ok:
            return None
        code, desc = res.get("_code"), res.get("description", "")
        last = f"HTTP {code} {desc}".strip()
        migrated = (res.get("parameters") or {}).get("migrate_to_chat_id")
        if migrated:  # 그룹이 슈퍼그룹으로 전환되어 chat id 가 바뀐 경우: 새 id 로 재시도
            print(f"::warning::대화방 chat id 가 {migrated} 로 변경됨 - Secrets 의 TELEGRAM_CHAT_ID 를 수정하십시오")
            params["chat_id"] = str(migrated)
            continue
        if code == 400 and "parse entities" in desc:  # 서식 오류: 태그 없는 일반 글로 재시도
            params.pop("parse_mode", None)
            params["text"] = html.unescape(re.sub(r"<[^>]+>", "", text))
            continue
        if code == 429:
            time.sleep(int((res.get("parameters") or {}).get("retry_after", 5)) + 1)
            continue
        if code in (400, 401, 403, 404):  # 토큰·chat id 문제는 재시도해도 소용없음
            break
        time.sleep(3 * (i + 1))
    return last


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chats = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
    if not token or not chats:
        raise SystemExit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 환경변수가 필요합니다.")
    failed = []
    for chat in chats:  # 한 곳이 실패해도 나머지 대화방에는 계속 발송
        err = send_one(token, chat, text)
        if err is None:
            print(f"발송 완료 (chat {chat[:4]}***)", flush=True)
        else:
            print(f"::error::발송 실패 (chat {chat[:4]}***): {err}", flush=True)
            failed.append(f"{chat[:4]}***: {err}")
    if failed:
        raise SystemExit("텔레그램 발송 실패 - " + " / ".join(failed))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="발송하지 않고 문구만 출력")
    ap.add_argument("--force", action="store_true", help="주말·공휴일이어도 진행")
    ap.add_argument("--no-wait", action="store_true", help="발송 시각까지 대기하지 않음")
    ap.add_argument("--now", help="시험용 현재 시각 'YYYY-MM-DD HH:MM' (KST)")
    ap.add_argument("--local", help="저장소를 clone 해 둔 로컬 폴더(시험용)")
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)
    now = datetime.strptime(args.now, "%Y-%m-%d %H:%M").replace(tzinfo=KST) if args.now else datetime.now(KST)

    # config.json 의 skip_off_days 가 true 일 때만 주말·공휴일을 건너뛴다(기본: 매일 발송)
    if cfg.get("skip_off_days", False) and not args.force:
        reason = is_off_day(now.date(), cfg, args.local)
        if reason:
            print(f"{md(now.date())} {reason} - 발송하지 않음")
            return

    # 발송 시각까지 대기 (대기 후 최신 데이터를 읽는다)
    if not args.no_wait and not args.now:
        hh, mm = map(int, cfg["send_time"].split(":"))
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        wait = (target - now).total_seconds()
        if 0 < wait <= 90 * 60:
            print(f"{cfg['send_time']} 까지 {int(wait // 60)}분 대기")
            time.sleep(wait)
            now = datetime.now(KST)
        elif wait > 90 * 60:
            print("발송 시각까지 90분 이상 남아 즉시 진행")

    ctx = Ctx(cfg, args.local, now)
    text = build_message(ctx)
    print(text)
    print(f"\n[글자수 {len(text)} / 경고 {len(ctx.errors)}건]")
    if args.dry_run:
        print("(dry-run: 발송하지 않음)")
        return
    send_telegram(text)


if __name__ == "__main__":
    main()
