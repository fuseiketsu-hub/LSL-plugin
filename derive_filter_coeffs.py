# -*- coding: utf-8 -*-
"""从零手工推导滤波系数：不调用 scipy 的 lp2bp/bilinear，只用多项式运算。

若能复现 scipy.butter 的结果，就说明"系数怎么得出的"这条链是真的。
跑法：python derive_filter_coeffs.py
"""
import numpy as np
from scipy import signal

FS, F1, F2, N = 1000.0, 20.0, 450.0, 4


def show(name, c, var):
    """按降幂打印多项式。"""
    c = np.asarray(c, dtype=float)
    terms = []
    deg = len(c) - 1
    for i, v in enumerate(c):
        k = deg - i
        if abs(v) < 1e-14:
            continue
        if k == 0:
            terms.append(f"{v:+.9e}")
        elif k == 1:
            terms.append(f"{v:+.9e}·{var}")
        else:
            terms.append(f"{v:+.9e}·{var}^{k}")
    print(f"  {name}（{deg} 次）:")
    print("    " + "  ".join(terms) if terms else "    0")


print("=" * 78)
print("从零手工推导：fs=1000 Hz，带通 20–450 Hz，Butterworth 4 阶")
print("=" * 78)

# ---------------------------------------------------------------- 第 1 步
print("\n【第 1 步】模拟低通原型  H_lp(s) = 1 / A_lp(s)，Ωc = 1")
poles = np.exp(1j * np.pi * (2 * np.arange(N) + N + 1) / (2 * N))
print(f"  极点 s_k = exp(jπ(2k+n+1)/(2n))，k=0..{N-1}:")
for s in sorted(poles, key=lambda z: z.imag):
    print(f"    {s.real:+.9f}{s.imag:+.9f}j")
A_lp = np.real(np.poly(poles))          # 降幂：s^4 + a3 s^3 + ...
B_lp = np.array([1.0])                  # 分子是常数 1
show("A_lp(s)", A_lp, "s")
print(f"  → 分母是把 4 个极点乘开：(s−s₁)(s−s₂)(s−s₃)(s−s₄)")

# ---------------------------------------------------------------- 第 2 步
print("\n【第 2 步】预畸变（数字边界 → 模拟角频率）")
Om1 = 2 * FS * np.tan(np.pi * F1 / FS)
Om2 = 2 * FS * np.tan(np.pi * F2 / FS)
Om0 = np.sqrt(Om1 * Om2)
Bw = Om2 - Om1
print(f"  Ω = 2·fs·tan(π·f/fs)")
print(f"  Ω1 = 2·{FS:g}·tan(π·{F1:g}/{FS:g}) = {Om1:.9f}")
print(f"  Ω2 = 2·{FS:g}·tan(π·{F2:g}/{FS:g}) = {Om2:.9f}")
print(f"  Ω0 = √(Ω1Ω2) = {Om0:.9f}      BW = Ω2−Ω1 = {Bw:.9f}")

# ---------------------------------------------------------------- 第 3 步
print("\n【第 3 步】低通 → 带通：把 s 换成 (s²+Ω0²)/(BW·s)")
print("  对 A_lp(s) = Σ a_k s^k 代入并通分 ×(BW·s)^n：")
print("    A_bp(s) = Σ_k  a_k · (BW·s)^(n−k) · (s²+Ω0²)^k")
print("  （分子 B_lp=1 同理 → B_bp(s) = (BW·s)^n，即 n 个零点在 s=0）")


def bp_poly(coeffs_desc, n, bw, om0):
    """把低通多项式（降幂）代入带通变换，返回降幂系数（次数 2n）。

    展开式：H_bp(s) = Σ_k c_k · (BW·s)^(n−k) · (s²+Ω0²)^k
      · k 是**原多项式**里该项的 s 次幂（分子次数可能小于 n）
      · 第 k 项的次数是 n+k，要落到 2n 次数组的第 (n−k) 位
    """
    deg = len(coeffs_desc) - 1
    out = np.zeros(2 * n + 1)
    for i, ck in enumerate(coeffs_desc):
        k = deg - i                                 # 原多项式里的 s 次幂
        if k < 0 or n - k < 0:
            continue
        quad = np.array([1.0])                      # (s²+Ω0²)^k
        for _ in range(k):
            quad = np.polymul(quad, [1.0, 0.0, om0 ** 2])
        shift = np.zeros(n - k + 1)                 # (BW·s)^(n−k)
        shift[0] = bw ** (n - k)
        term = np.polymul(quad, shift)              # 降幂，次数 n+k
        out[n - k: n - k + len(term)] += ck * term  # 按最高次对齐
    return out


A_bp = bp_poly(A_lp, N, Bw, Om0)
B_bp = bp_poly(B_lp, N, Bw, Om0)
show("B_bp(s)", B_bp, "s")
show("A_bp(s)", A_bp, "s")
nz = np.nonzero(np.abs(B_bp) > 1e-12)[0]
print(f"  阶数：分子 {len(B_bp) - 1 - nz[0]} 次，分母 {len(A_bp)-1} 次"
      f"  → 阶数已翻倍（{N} → {2*N}）")

# 与 scipy 的 lp2bp 对拍（模拟域；补齐长度再比）
b_lp, a_lp = signal.butter(N, 1.0, analog=True)
b_bp_ref, a_bp_ref = signal.lp2bp(b_lp, a_lp, wo=Om0, bw=Bw)


def pad_desc(c, n):
    c = np.asarray(c, dtype=float)
    return np.concatenate([np.zeros(n - len(c)), c])


L = 2 * N + 1
d_b = np.max(np.abs(pad_desc(B_bp, L) - pad_desc(b_bp_ref, L))) / np.max(np.abs(b_bp_ref))
d_a = np.max(np.abs(pad_desc(A_bp, L) - pad_desc(a_bp_ref, L))) / np.max(np.abs(a_bp_ref))
print(f"  与 scipy.lp2bp 对比（相对偏差）：B_bp {d_b:.3e}   A_bp {d_a:.3e}"
      f"   {'✓ 一致' if max(d_b, d_a) < 1e-12 else '✗ 不一致'}")

# ---------------------------------------------------------------- 第 4 步
print("\n【第 4 步】双线性变换：s = 2·fs·(1−u)/(1+u)，u = z⁻¹")
print("  代进 H(s)=B_bp(s)/A_bp(s) 并通分，得到**同一个**公式：")
print("    P(u) = Σ_k  c_k · (2·fs)^k · (1−u)^k · (1+u)^(m−k)      m = 分母阶数")
print("  （分子那几个 s=∞ 的零点就是通分时 (1+u) 因子带来的 → 落在 z=−1）")


def bilinear(coeffs_desc, m, fs):
    """把模拟多项式（降幂，次数 ≤ m）经双线性变换成 u 的多项式（升幂返回）。"""
    out = np.zeros(m + 1)
    deg = len(coeffs_desc) - 1
    for i, ck in enumerate(coeffs_desc):
        k = deg - i
        minus = np.array([1.0])                     # (1−u)^k
        for _ in range(k):
            minus = np.polymul(minus, [1.0, -1.0])
        plus = np.array([1.0])                      # (1+u)^(m−k)
        for _ in range(m - k):
            plus = np.polymul(plus, [1.0, 1.0])
        out += ck * (2 * fs) ** k * np.polymul(minus, plus)   # 升幂
    return out


m = len(A_bp) - 1                    # = 8
Bz = bilinear(B_bp, m, FS)           # 升幂（u^0 在前），与 scipy 的 b 顺序一致
Az = bilinear(A_bp, m, FS)

print(f"\n  通分后：B(z) 分子 {len(Bz)} 项，A(z) 分母 {len(Az)} 项")
print(f"  归一化前 a0 = {Az[0]:.9e}")
Bz_n = Bz / Az[0]
Az_n = Az / Az[0]
show("B(z)（降幂）", Bz_n[::-1], "z⁻¹")
show("A(z)（降幂）", Az_n[::-1], "z⁻¹")

# ---------------------------------------------------------------- 验证
print("\n【验证】与 scipy.butter(output='ba') 对比")
b_ref, a_ref = signal.butter(N, [F1, F2], btype="bandpass", fs=FS, output="ba")
db = np.max(np.abs(Bz_n - b_ref))
da = np.max(np.abs(Az_n - a_ref))
print(f"  max|ΔB| = {db:.3e}    max|ΔA| = {da:.3e}")
print(f"  {'✓ 手工推导与 scipy 完全一致' if max(db, da) < 1e-12 else '✗ 不一致'}")
print(f"  B(z) 首项（= 后面会看到的那个 b0 总增益）= {Bz_n[0]:.12f}")

# ---------------------------------------------------------------- 第 5 步
print("\n【第 5 步】因式分解成二阶节（这一步才出现 b0/b1/b2/a1/a2）")
zeros = np.roots(Bz_n)
polz = np.roots(Az_n)
print(f"  分子零点 {len(zeros)} 个，分母极点 {len(polz)} 个")


def pair_roots(roots):
    """把共轭对配成二阶节，返回每个二阶节的 (b0,b1,b2)（归一化首项 1）。"""
    rem = list(roots)
    secs = []
    while rem:
        r = rem.pop(0)
        j = next((i for i, o in enumerate(rem) if abs(o - np.conj(r)) < 1e-6), None)
        if j is None:
            secs.append(np.array([1.0, -r.real]))            # 实根 → 一阶
        else:
            c = rem.pop(j)
            secs.append(np.array([1.0, -(r + c).real, (r * c).real]))
    return secs


print("  配对后的二阶节（未加总增益）：")
for i, (zs, ps) in enumerate(zip(pair_roots(zeros), pair_roots(polz))):
    zs = np.pad(zs, (0, 3 - len(zs))) if len(zs) < 3 else zs
    ps = np.pad(ps, (0, 3 - len(ps))) if len(ps) < 3 else ps
    if len(zs) == 3 and len(ps) == 3:
        print(f"    节#{i}: b=[{zs[0]: .9f} {zs[1]: .9f} {zs[2]: .9f}]"
              f"  a=[1 {ps[1]: .9f} {ps[2]: .9f}]")

sos_ref = signal.butter(N, [F1, F2], btype="bandpass", fs=FS, output="sos")
print("\n  scipy 的 SOS（它把总增益集中在第 0 节）：")
for i, r in enumerate(sos_ref):
    print(f"    节#{i}: b=[{r[0]: .9f} {r[1]: .9f} {r[2]: .9f}]"
          f"  a=[1 {r[4]: .9f} {r[5]: .9f}]")
print(f"  各节 b0 乘积 = {np.prod(sos_ref[:,0]):.12f} = B(z) 首项 ✓")

# ---------------------------------------------------------------- 陷波
print("\n【陷波】二阶闭式，直接算（无需变换链）")
f0, Q = 50.0, 30.0
w0 = 2 * np.pi * f0 / FS
beta = np.tan(w0 / (2 * Q))
g = 1 / (1 + beta)
print(f"  w0 = 2π·{f0:g}/{FS:g} = {w0:.12f}")
print(f"  β = tan(w0/(2Q)) = tan({w0/(2*Q):.12f}) = {beta:.12f}")
print(f"  gain = 1/(1+β) = {g:.12f}")
print(f"  b = gain·[1, −2cos w0, 1] = [{g:.12f}, {-2*g*np.cos(w0):.12f}, {g:.12f}]")
print(f"  a = [1, −2·gain·cos w0, 2·gain−1] = [1, {-2*g*np.cos(w0):.12f},"
      f" {2*g-1:.12f}]")
bn, an = signal.iirnotch(f0, Q, fs=FS)
print(f"  scipy 实际: b=[{bn[0]:.12f}, {bn[1]:.12f}, {bn[2]:.12f}]"
      f"  a=[1, {an[1]:.12f}, {an[2]:.12f}]")
print(f"  差 {max(np.max(np.abs([g,-2*g*np.cos(w0),g]-bn)), np.max(np.abs([1,-2*g*np.cos(w0),2*g-1]-an))):.3e} ✓")

print("\n" + "=" * 78)
print("结论：b0 等系数 = 上面 5 步代数运算的结果；SOS 只是第 5 步的分解形式。")
print("=" * 78)
