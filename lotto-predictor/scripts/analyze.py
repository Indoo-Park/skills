#!/usr/bin/env python3
"""로또6/45 과거 당첨번호 통계 분석 + 다음 회차 추천 조합 생성 (표준 라이브러리만 사용).

입력은 fetch_draws.py 가 만든 캐시 JSON. 계산 내용은 references/methodology.md 참고.

  1. 균일성 검정    : 전체/최근 구간 출현 횟수에 대한 카이제곱 적합도 검정
  2. 번호별 통계    : 출현 횟수, 최근 구간 z-점수(이항), 지수가중 출현량, 미출현 간격
  3. 가중치         : 지수가중 출현량 + 균등 사전분포(베이지안 수축) → 번호별 사후 출현확률
  4. 구조 필터      : 과거 당첨조합의 합계·홀짝·저고·연속·구간 분포에서 벗어난 조합 제외
  5. 조합 생성      : 가중치 비복원 추출 + 필터 + 세트 간 중복 제한
  6. 백테스트       : 과거 K회차를 '그 시점까지의 데이터만으로' 재현해 무작위 대비 성능 비교

사용 예:
    python analyze.py --data ~/.cache/lotto-predictor/draws.json
    python analyze.py --data draws.json --recent 50 --halflife 15 --sets 5 --strategy mixed --seed 7
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

N_BALLS = 45
PICK = 6
P_HIT = PICK / N_BALLS  # 한 회차에 특정 번호가 나올 확률 = 6/45
BANDS = [(1, 10), (11, 20), (21, 30), (31, 40), (41, 45)]
DEFAULT_DATA = Path.home() / ".cache" / "lotto-predictor" / "draws.json"


# ---------------------------------------------------------------- 수학 유틸
def gammaincc(a: float, x: float) -> float:
    """정규화 상부 불완전 감마 Q(a, x). 카이제곱 p-값 = Q(df/2, chi2/2)."""
    if x <= 0:
        return 1.0
    gln = math.lgamma(a)
    if x < a + 1:  # 급수 전개
        ap, s, delta = a, 1.0 / a, 1.0 / a
        for _ in range(1000):
            ap += 1
            delta *= x / ap
            s += delta
            if abs(delta) < abs(s) * 1e-14:
                break
        return 1.0 - s * math.exp(-x + a * math.log(x) - gln)
    b, c, d = x + 1 - a, 1e300, 1.0 / (x + 1 - a)  # 연분수 전개
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = 1e-300 if abs(d) < 1e-300 else d
        c = b + an / c
        c = 1e-300 if abs(c) < 1e-300 else c
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1) < 1e-14:
            break
    return math.exp(-x + a * math.log(x) - gln) * h


def chi2_uniform(counts: list[int], n_draws: int) -> dict:
    expected = n_draws * P_HIT
    chi2 = sum((c - expected) ** 2 / expected for c in counts)
    df = N_BALLS - 1
    return {"draws": n_draws, "chi2": round(chi2, 2), "df": df,
            "p_value": round(gammaincc(df / 2, chi2 / 2), 4), "expected_per_number": round(expected, 2)}


def hypergeom_match_probs() -> list[float]:
    """구입한 6개 중 k개가 당첨번호와 일치할 확률 (k=0..6)."""
    total = math.comb(N_BALLS, PICK)
    return [math.comb(PICK, k) * math.comb(N_BALLS - PICK, PICK - k) / total for k in range(PICK + 1)]


def quantile(sorted_vals: list[float], q: float) -> float:
    i = (len(sorted_vals) - 1) * q
    lo, hi = math.floor(i), math.ceil(i)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (i - lo)


# ---------------------------------------------------------------- 번호별 통계
def number_stats(draws: list[dict], recent: int, halflife: float, prior_draws: float) -> dict[int, dict]:
    n = len(draws)
    lam = 0.5 ** (1 / halflife)
    full = Counter()
    rec = Counter()
    ewma = Counter()
    last_seen: dict[int, int] = {}
    max_gap = Counter()
    prev_seen: dict[int, int] = {}
    eff_n = 0.0
    for idx, d in enumerate(draws):
        age = n - 1 - idx
        w = lam ** age
        eff_n += w
        for x in d["numbers"]:
            full[x] += 1
            ewma[x] += w
            if age < recent:
                rec[x] += 1
            if x in prev_seen:
                max_gap[x] = max(max_gap[x], idx - prev_seen[x] - 1)
            prev_seen[x] = idx
            last_seen[x] = d["draw"]
    window = min(recent, n)
    sd = math.sqrt(window * P_HIT * (1 - P_HIT))
    alpha = prior_draws * P_HIT
    latest = draws[-1]["draw"]
    out = {}
    for x in range(1, N_BALLS + 1):
        z = (rec[x] - window * P_HIT) / sd if sd else 0.0
        gap = latest - last_seen.get(x, 0)
        post = (alpha + ewma[x]) / (prior_draws + eff_n)  # 베타-이항 사후평균 (회차당 출현확률)
        out[x] = {
            "full_count": full[x],
            "recent_count": rec[x],
            "recent_z": round(z, 2),
            "recent_p_two_sided": round(math.erfc(abs(z) / math.sqrt(2)), 4),
            "ewma": round(ewma[x], 3),
            "posterior_rate": round(post, 4),
            "lift": round(post / P_HIT, 3),
            "gap": gap,
            "max_gap": max(max_gap[x], gap),
            "p_gap_at_least": round((1 - P_HIT) ** gap, 4),  # 무작위라면 이만큼 이상 안 나올 확률
        }
    return out


# ---------------------------------------------------------------- 구조 필터
def combo_features(nums: list[int]) -> dict:
    s = sorted(nums)
    run = best = 1
    for a, b in zip(s, s[1:]):
        run = run + 1 if b == a + 1 else 1
        best = max(best, run)
    band_max = max(sum(lo <= x <= hi for x in s) for lo, hi in BANDS)
    return {"sum": sum(s), "odd": sum(x % 2 for x in s), "low": sum(x <= 22 for x in s),
            "max_run": best, "band_max": band_max}


def structure_profile(draws: list[dict]) -> dict:
    feats = [combo_features(d["numbers"]) for d in draws]
    n = len(feats)
    sums = sorted(f["sum"] for f in feats)

    def common_values(key: str, min_share: float = 0.10) -> list[int]:
        c = Counter(f[key] for f in feats)
        return sorted(v for v, k in c.items() if k / n >= min_share)

    def cover_95(key: str) -> int:
        c = Counter(f[key] for f in feats)
        acc = 0
        for v in sorted(c):
            acc += c[v]
            if acc / n >= 0.95:
                return v
        return max(c)

    prof = {
        "sum_range": [round(quantile(sums, 0.05)), round(quantile(sums, 0.95))],
        "odd_allowed": common_values("odd"),
        "low_allowed": common_values("low"),
        "max_run_limit": cover_95("max_run"),
        "band_max_limit": cover_95("band_max"),
    }
    prof["historical_pass_rate"] = round(sum(passes(f, prof) for f in feats) / n, 3)
    return prof


def passes(f: dict, prof: dict) -> bool:
    lo, hi = prof["sum_range"]
    return (lo <= f["sum"] <= hi and f["odd"] in prof["odd_allowed"] and f["low"] in prof["low_allowed"]
            and f["max_run"] <= prof["max_run_limit"] and f["band_max"] <= prof["band_max_limit"])


# ---------------------------------------------------------------- 가중치 / 생성
def sampling_weights(stats: dict[int, dict], strategy: str, sharpen: float = 1.0) -> dict[int, float]:
    # lift^sharpen: 사후확률 차이는 보통 ±30% 이내라 그대로 쓰면 균등추출과 거의 같다.
    # sharpen 을 키우면 강세 번호가 조합에 더 자주 들어간다 (확률적 근거가 강해지는 것은 아님).
    hot = {x: s["lift"] ** sharpen for x, s in stats.items()}
    if strategy == "uniform":
        return {x: 1.0 for x in stats}
    if strategy == "hot":
        return hot
    # mixed: 최근 강세(70%) + 장기 미출현(30%)
    hot_sum = sum(hot.values())
    gap_sum = sum(s["gap"] + 1 for s in stats.values())
    return {x: 0.7 * hot[x] / hot_sum + 0.3 * (stats[x]["gap"] + 1) / gap_sum for x in stats}


def weighted_pick(weights: dict[int, float], rng: random.Random) -> list[int]:
    pool = dict(weights)
    chosen = []
    for _ in range(PICK):
        keys = list(pool)
        x = rng.choices(keys, weights=[pool[k] for k in keys])[0]
        chosen.append(x)
        del pool[x]
    return sorted(chosen)


def number_pools(stats: dict[int, dict], n_hot: int = 10, n_overdue: int = 6) -> dict[str, list[int]]:
    """최근 강세(최근 구간 출현 횟수, 동률이면 lift) / 장기 미출현(gap) 상위 번호."""
    hot = sorted(stats, key=lambda x: (-stats[x]["recent_count"], -stats[x]["lift"]))[:n_hot]
    overdue = sorted(stats, key=lambda x: -stats[x]["gap"])[:n_overdue]
    return {"hot": hot, "overdue": overdue}


def generate_sets(weights: dict[int, float], prof: dict, n_sets: int, rng: random.Random,
                  pools: dict[str, list[int]] | None = None, min_hot: int = 0, min_overdue: int = 0,
                  max_overlap: int = 3, max_tries: int = 50000) -> list[list[int]]:
    hot = set(pools["hot"]) if pools else set()
    overdue = set(pools["overdue"]) if pools else set()
    sets: list[list[int]] = []
    for _ in range(max_tries):
        if len(sets) >= n_sets:
            break
        c = weighted_pick(weights, rng)
        if not passes(combo_features(c), prof):
            continue
        # 사용자가 원한 성향(강세 위주, 미출현 섞기)이 추첨 운에 묻히지 않도록 최소 포함 개수를 보장
        if len(hot & set(c)) < min_hot or len(overdue & set(c)) < min_overdue:
            continue
        if any(len(set(c) & set(s)) > max_overlap for s in sets):
            continue
        sets.append(c)
    return sets


# ---------------------------------------------------------------- 백테스트
def backtest(draws: list[dict], k: int, n_sets: int, args, seed: int) -> dict:
    k = min(k, len(draws) - 100)  # 최소 100회차 이력은 확보
    if k <= 0:
        return {"skipped": "이력이 부족합니다."}
    rng_s, rng_b = random.Random(seed), random.Random(seed + 1)
    strat_hits, base_hits = Counter(), Counter()
    for t in range(len(draws) - k, len(draws)):
        hist, target = draws[:t], set(draws[t]["numbers"])
        prof = structure_profile(hist)
        st = number_stats(hist, args.recent, args.halflife, args.prior_draws)
        for c in generate_sets(sampling_weights(st, args.strategy, args.sharpen), prof, n_sets, rng_s,
                               number_pools(st), args.min_hot, args.min_overdue):
            strat_hits[len(target & set(c))] += 1
        for c in generate_sets(sampling_weights(st, "uniform"), prof, n_sets, rng_b):
            base_hits[len(target & set(c))] += 1

    def summarize(h: Counter) -> dict:
        tot = sum(h.values())
        mean = sum(m * c for m, c in h.items()) / tot
        var = sum(c * (m - mean) ** 2 for m, c in h.items()) / max(tot - 1, 1)
        return {"tickets": tot, "mean_matches": round(mean, 4), "se": round(math.sqrt(var / tot), 4),
                "rate_3plus": round(sum(c for m, c in h.items() if m >= 3) / tot, 4),
                "distribution": {m: h.get(m, 0) for m in range(PICK + 1)}}

    s, b = summarize(strat_hits), summarize(base_hits)
    diff = s["mean_matches"] - b["mean_matches"]
    se = math.sqrt(s["se"] ** 2 + b["se"] ** 2)
    probs = hypergeom_match_probs()
    return {"draws_tested": k, "strategy": s, "uniform_baseline": b,
            "theory_mean_matches": round(PICK * PICK / N_BALLS, 4),
            "theory_rate_3plus": round(sum(probs[3:]), 4),
            "diff_mean": round(diff, 4), "diff_z": round(diff / se, 2) if se else 0.0}


# ---------------------------------------------------------------- 출력
def render(r: dict) -> str:
    L = []
    m = r["meta"]
    L.append(f"# 로또6/45 통계 분석 — 제{m['next_draw']}회 ({m['next_date']} 추첨 예정)\n")
    L.append(f"- 데이터: 제1회 ~ 제{m['latest_draw']}회 ({m['latest_date']}), 총 {m['n_draws']}회차")
    L.append(f"- 설정: 최근 구간 {m['recent']}회 · 반감기 {m['halflife']}회 · 사전강도 {m['prior_draws']}회 · "
             f"전략 `{m['strategy']}` · 강조 {m['sharpen']} · seed {m['seed']}\n")

    L.append("## 1. 무작위성 검정 (카이제곱 적합도)")
    for label, key in (("전체 이력", "chi2_full"), (f"최근 {m['recent']}회", "chi2_recent")):
        c = r[key]
        verdict = "균등분포와 유의한 차이 없음" if c["p_value"] >= 0.05 else "5% 수준에서 균등분포와 차이"
        L.append(f"- {label}: χ²={c['chi2']} (df={c['df']}), p={c['p_value']} → {verdict}")
    L.append("")

    st = r["numbers"]
    bonf = 0.05 / N_BALLS
    hot = r["pools"]["hot"]
    cold = sorted(st, key=lambda x: st[x]["recent_z"])[:5]
    overdue = r["pools"]["overdue"]
    L.append(f"## 2. 최근 {m['recent']}회 강세 번호 상위 10 (번호당 기대값 {r['chi2_recent']['expected_per_number']}회)")
    L.append("| 번호 | 최근 | 전체 | z | p(양측) | 사후확률 lift | 미출현 |")
    L.append("|---:|---:|---:|---:|---:|---:|---:|")
    for x in hot:
        s = st[x]
        flag = " *" if s["recent_p_two_sided"] < bonf else ""
        L.append(f"| {x}{flag} | {s['recent_count']} | {s['full_count']} | {s['recent_z']:+.2f} | "
                 f"{s['recent_p_two_sided']} | {s['lift']:.3f} | {s['gap']}회 |")
    n_sig = sum(st[x]["recent_p_two_sided"] < bonf for x in st)
    L.append(f"\n본페로니 보정 기준(p<{bonf:.4f})을 넘는 번호: {n_sig}개" + (" (* 표시)" if n_sig else ""))
    L.append(f"- 최근 저조: " + ", ".join(f"{x}({st[x]['recent_count']}회, z={st[x]['recent_z']:+.2f})" for x in cold))
    L.append(f"- 장기 미출현 상위 {len(overdue)}: " + ", ".join(f"{x}({st[x]['gap']}회째, 무작위 확률 {st[x]['p_gap_at_least']})"
                                          for x in overdue) + "\n")

    p = r["profile"]
    L.append("## 3. 조합 구조 필터 (과거 당첨조합 기준)")
    L.append(f"- 합계 {p['sum_range'][0]}~{p['sum_range'][1]} · 홀수 개수 {p['odd_allowed']} · "
             f"22 이하 개수 {p['low_allowed']} · 최대 연속 {p['max_run_limit']}개 · 한 구간 최대 {p['band_max_limit']}개")
    L.append(f"- 과거 당첨조합 중 이 필터를 통과하는 비율: {p['historical_pass_rate']*100:.1f}%\n")

    L.append(f"## 4. 제{m['next_draw']}회 추천 조합")
    L.append("추출 가중치 상위 10개: " + ", ".join(f"{t['number']}({t['share']*100:.1f}%)" for t in r["top_weighted"])
             + f"  — 균등이면 각 {100/N_BALLS:.1f}%\n")
    for i, c in enumerate(r["sets"], 1):
        f = combo_features(c)
        L.append(f"{i}. **{' · '.join(f'{x:02d}' for x in c)}**  (합 {f['sum']}, 홀{f['odd']}:짝{PICK - f['odd']})")
    if len(r["sets"]) < m["n_sets_requested"]:
        L.append(f"\n> 조건을 만족하는 조합을 {len(r['sets'])}개만 찾았습니다. --min-hot/--min-overdue 를 낮춰 보세요.")
    L.append(f"\n게임마다 보장: 강세 상위 10 중 ≥{m['min_hot']}개, 장기 미출현 상위 6 중 ≥{m['min_overdue']}개\n")
    L.append("### 선택된 번호별 근거")
    L.append(f"| 번호 | 구분 | 최근 {m['recent']}회 | 사후확률 lift | 미출현 | 추출 가중치 |")
    L.append("|---:|---|---:|---:|---:|---:|")
    for d in r["picked_detail"]:
        L.append(f"| {d['number']} | {'·'.join(d['tags']) or '일반(가중 추첨)'} | {d['recent_count']}회 | "
                 f"{d['lift']:.3f} | {d['gap']}회 | {d['share']*100:.1f}% |")
    L.append("")

    bt = r["backtest"]
    L.append("## 5. 백테스트 (과거 데이터로 이 방법을 검증)")
    if "skipped" in bt:
        L.append(f"- {bt['skipped']}")
    else:
        s, b = bt["strategy"], bt["uniform_baseline"]
        L.append(f"- 최근 {bt['draws_tested']}회차를 각 시점 이전 데이터만으로 재현, 회차당 {m['n_sets_requested']}게임")
        L.append(f"- 평균 일치 개수: 이 방법 {s['mean_matches']} (±{s['se']}) · 무작위 {b['mean_matches']} (±{b['se']}) · "
                 f"이론값 {bt['theory_mean_matches']}")
        L.append(f"- 3개 이상 일치(5등 이상) 비율: 이 방법 {s['rate_3plus']*100:.2f}% · 무작위 {b['rate_3plus']*100:.2f}% · "
                 f"이론값 {bt['theory_rate_3plus']*100:.2f}%")
        L.append(f"- 차이 z = {bt['diff_z']} → " + ("통계적으로 의미 있는 차이 없음" if abs(bt["diff_z"]) < 2
                                                  else "차이가 관측됨 (표본·다중비교 주의)"))
    return "\n".join(L)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="로또6/45 통계 분석 및 추천 조합 생성")
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA, help="fetch_draws.py 캐시 JSON")
    ap.add_argument("--recent", type=int, default=100, help="'최근' 구간 회차 수 (기본 100)")
    ap.add_argument("--halflife", type=float, default=20, help="지수가중 반감기, 회차 단위 (기본 20)")
    ap.add_argument("--prior-draws", type=float, default=50, help="균등 사전분포 강도, 가상 회차 수 (기본 50)")
    ap.add_argument("--strategy", choices=["hot", "mixed", "uniform"], default="hot")
    ap.add_argument("--sharpen", type=float, default=3.0, help="강세 번호 강조 지수, 1이면 사후확률 그대로 (기본 3)")
    ap.add_argument("--min-hot", type=int, default=None,
                    help="게임마다 최근 강세 상위 10개 중 최소 포함 개수 (기본: hot/mixed 2, uniform 0)")
    ap.add_argument("--min-overdue", type=int, default=None,
                    help="게임마다 장기 미출현 상위 6개 중 최소 포함 개수 (기본: mixed 1, 그 외 0)")
    ap.add_argument("--sets", type=int, default=5, help="추천 게임 수 (기본 5)")
    ap.add_argument("--backtest", type=int, default=100, help="백테스트 회차 수, 0이면 생략 (기본 100)")
    ap.add_argument("--seed", type=int, default=None, help="재현용 시드 (기본: 다음 회차 번호)")
    ap.add_argument("--json", type=Path, help="전체 결과를 JSON으로도 저장")
    args = ap.parse_args()
    if args.min_hot is None:
        args.min_hot = 0 if args.strategy == "uniform" else 2
    if args.min_overdue is None:
        args.min_overdue = 1 if args.strategy == "mixed" else 0

    if not args.data.exists():
        print(f"데이터 파일이 없습니다: {args.data}\n먼저 fetch_draws.py 를 실행하세요.", file=sys.stderr)
        return 1
    draws = sorted(json.loads(args.data.read_text(encoding="utf-8"))["draws"], key=lambda d: d["draw"])
    if len(draws) < 50:
        print("분석하기에 회차가 너무 적습니다 (50회 미만).", file=sys.stderr)
        return 1

    latest = draws[-1]
    next_draw = latest["draw"] + 1
    seed = args.seed if args.seed is not None else next_draw
    recent = min(args.recent, len(draws))
    stats = number_stats(draws, recent, args.halflife, args.prior_draws)
    prof = structure_profile(draws)
    weights = sampling_weights(stats, args.strategy, args.sharpen)
    pools = number_pools(stats)
    sets = generate_sets(weights, prof, args.sets, random.Random(seed), pools, args.min_hot, args.min_overdue)
    w_sum = sum(weights.values())
    top_weighted = sorted(weights, key=lambda x: -weights[x])[:10]
    picked = sorted({x for s in sets for x in s})
    picked_detail = [{"number": x, "share": round(weights[x] / w_sum, 4),
                      "recent_count": stats[x]["recent_count"], "lift": stats[x]["lift"], "gap": stats[x]["gap"],
                      "tags": [t for t, pool in (("강세", pools["hot"]), ("미출현", pools["overdue"])) if x in pool]}
                     for x in picked]
    full_counts = [stats[x]["full_count"] for x in range(1, N_BALLS + 1)]
    recent_counts = [stats[x]["recent_count"] for x in range(1, N_BALLS + 1)]

    result = {
        "meta": {"latest_draw": latest["draw"], "latest_date": latest["date"], "n_draws": len(draws),
                 "next_draw": next_draw,
                 "next_date": (date.fromisoformat(latest["date"]) + timedelta(days=7)).isoformat(),
                 "recent": recent, "halflife": args.halflife, "prior_draws": args.prior_draws,
                 "strategy": args.strategy, "sharpen": args.sharpen, "seed": seed,
                 "min_hot": args.min_hot, "min_overdue": args.min_overdue, "n_sets_requested": args.sets},
        "chi2_full": chi2_uniform(full_counts, len(draws)),
        "chi2_recent": chi2_uniform(recent_counts, recent),
        "numbers": stats,
        "profile": prof,
        "top_weighted": [{"number": x, "share": round(weights[x] / w_sum, 4)} for x in top_weighted],
        "pools": pools,
        "sets": sets,
        "picked_detail": picked_detail,
        "backtest": backtest(draws, args.backtest, args.sets, args, seed) if args.backtest > 0
        else {"skipped": "--backtest 0"},
    }
    print(render(result))
    if args.json:
        args.json.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
