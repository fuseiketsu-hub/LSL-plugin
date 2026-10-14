# -*- coding: utf-8 -*-
"""核对滤波系数：把 b0/a1 这些数字是怎么来的逐步复现一遍。

跑法：python _check_filter_math.py
依赖 scipy>=1.17（lp2bp 只支持 (b,a) 形式）
"""
import numpy as np
from scipy import signal

FS, HP, LP, ORDER, F0, Q = 1000.0, 20.0, 450.0, 4, 50.0, 30.0

print("=" * 76)
print(f"配置：fs={FS:g} Hz   带通 {HP:g}–{LP:g} Hz   Butterworth {ORDER} 阶"
      f"   陷波 {F0:g} Hz Q={Q:g}")
print("=" * 76)

# ---------------------------------------------------------------- 1) 实际系数
sos_bp = signal.butter(ORDER, [HP, LP], btype="bandpass", fs=FS, output="sos")
bn, an = signal.iirnotch(F0, Q, fs=FS)
sos = np.vstack([sos_bp, signal.tf2sos(bn, an)])

print(f"\n[1] 实际级联的系数（{sos.shape[0]} 个二阶节，SOS 形式）")
print("    每行 = [b0, b1, b2, a0=1, a1, a2]，a0 已归一化为 1")
for i, row in enumerate(sos):
    tag = "带通" if i < ORDER else "陷波"
    print(f"    {tag}#{i}  b0={row[0]: .12f}  b1={row[1]: .12f}  b2={row[2]: .12f}")
    print(f"           a0=1  a1={row[4]: .12f}  a2={row[5]: .12f}")

# ------------------------------------------------------------ 2) 带通：完整推导
print("\n" + "-" * 76)
print("[2] 带通 b0 的来源：模拟原型 → 预畸变 → 低通转带通 → 双线性变换 → 配对")
print("-" * 76)

b_lp, a_lp = signal.butter(ORDER, 1.0, analog=True)
p_lp = np.roots(a_lp)
print(f"  ① 模拟低通原型（−3dB @ Ωc=1 rad/s，{ORDER} 阶 Butterworth）")
print(f"     幅频平方：|H(jΩ)|² = 1 / (1 + (Ω/Ωc)^(2n))")
print(f"     极点公式：s_k = Ωc · exp(j·π·(2k+n+1)/(2n))，k=0..n−1")
for s in sorted(p_lp, key=lambda z: z.imag):
    print(f"       {s.real:+.9f}{s.imag:+.9f}j   |s|={abs(s):.9f}"
          f"   角度 {np.degrees(np.arctan2(s.imag, s.real)):+8.3f}°")
print("     → 极点全部落在左半平面、半径为 Ωc 的圆上（这就是 Butterworth 的定义）")

W1 = 2.0 * FS * np.tan(np.pi * HP / FS)
W2 = 2.0 * FS * np.tan(np.pi * LP / FS)
Wo = np.sqrt(W1 * W2)
Bw = W2 - W1
print(f"\n  ② 预畸变（pre-warping）：数字边界 → 模拟角频率")
print(f"     公式：Ω = 2·fs·tan(π·f/fs)     ← 不是 2π·f，tan 才是双线性的逆")
print(f"     Ω1 = 2·{FS:g}·tan(π·{HP:g}/{FS:g}) = {W1:.6f} rad/s")
print(f"     Ω2 = 2·{FS:g}·tan(π·{LP:g}/{FS:g}) = {W2:.6f} rad/s")
print(f"     对比：2π·{HP:g}={2*np.pi*HP:.4f}（差 {abs(W1-2*np.pi*HP)/W1*100:.4f}%）"
      f"   2π·{LP:g}={2*np.pi*LP:.4f}（差 {abs(W2-2*np.pi*LP)/W2*100:.3f}%）")
print(f"     → 靠近奈奎斯特的高边差得很多，所以必须用 tan 预畸变")
print(f"     中心 Ω0 = √(Ω1·Ω2) = {Wo:.6f} rad/s")
print(f"     带宽 BW  = Ω2−Ω1    = {Bw:.6f} rad/s")

b_bp, a_bp = signal.lp2bp(b_lp, a_lp, wo=Wo, bw=Bw)
print(f"\n  ③ 低通 → 带通：s → (s² + Ω0²) / (BW·s)")
print(f"     阶数翻倍：分子 {len(b_lp)-1} 次 → {len(b_bp)-1} 次；"
      f"分母 {len(a_lp)-1} 次 → {len(a_bp)-1} 次")

b_d, a_d = signal.bilinear(b_bp, a_bp, FS)
print(f"\n  ④ 双线性变换：s = 2·fs·(1−z⁻¹)/(1+z⁻¹)")
print(f"     得到数字滤波器 b（{len(b_d)} 项）、a（{len(a_d)} 项）")

b_ref, a_ref = signal.butter(ORDER, [HP, LP], btype="bandpass", fs=FS, output="ba")
print(f"\n  ⑤ 与 scipy.butter(output='ba') 对比：")
print(f"     max|Δb| = {np.max(np.abs(b_d-b_ref)):.3e}   "
      f"max|Δa| = {np.max(np.abs(a_d-a_ref)):.3e}")
ok_ba = max(np.max(np.abs(b_d-b_ref)), np.max(np.abs(a_d-a_ref))) < 1e-10
print(f"     {'✓ 完全一致：系数就是这条推导链算出来的' if ok_ba else '✗ 不一致'}")

sos_manual = signal.tf2sos(b_d, a_d)
w, h_ref = signal.sosfreqz(sos_bp, worN=4096, fs=FS)
_, h_man = signal.sosfreqz(sos_manual, worN=4096, fs=FS)
print(f"     转成 SOS 后频响最大差：{np.max(np.abs(h_man-h_ref)):.3e}"
      "（SOS 的分节配对方式可与 scipy 不同，但整体传函相同）")

# ------------------------------------------------------------ 3) 陷波
print("\n" + "-" * 76)
print("[3] 陷波 b0 的来源：两个零点精确放在单位圆的 e^{±jw0} 上")
print("-" * 76)
w0 = 2 * np.pi * F0 / FS
cos_w0 = np.cos(w0)
alpha = np.sin(w0) / (2 * Q)
print(f"  w0    = 2π·f0/fs = {w0:.12f} rad")
print(f"  cos w0= {cos_w0:.12f}")
print(f"  alpha = sin(w0)/(2Q) = {alpha:.12f}")

gain_rbj = 1.0 / (1.0 + alpha)                      # RBJ：alpha = sin(w0)/(2Q)
b_rbj = gain_rbj * np.array([1.0, -2.0 * cos_w0, 1.0])
a_rbj = np.array([1.0, -2.0 * gain_rbj * cos_w0, 2.0 * gain_rbj - 1.0])

beta = np.tan(w0 / (2.0 * Q))                       # Orfanidis：beta = tan(w0/(2Q))
gain_or = 1.0 / (1.0 + beta)
b_or = gain_or * np.array([1.0, -2.0 * cos_w0, 1.0])
a_or = np.array([1.0, -2.0 * gain_or * cos_w0, 2.0 * gain_or - 1.0])

print(f"\n  beta = tan(w0/(2Q)) = {beta:.12f}   （Orfanidis）")
print(f"  alpha = sin(w0)/(2Q) = {alpha:.12f}   （RBJ cookbook）")
print(f"  → 两者不同：{abs(beta-alpha):.3e}，所以要用对哪一个")
print(f"\n  RBJ 闭式       b0={b_rbj[0]:.12f}  a1={a_rbj[1]:.12f}  a2={a_rbj[2]:.12f}")
print(f"  Orfanidis 闭式 b0={b_or[0]:.12f}  a1={a_or[1]:.12f}  a2={a_or[2]:.12f}")
print(f"  scipy 实际     b0={bn[0]:.12f}  a1={an[1]:.12f}  a2={an[2]:.12f}")
d_rbj = max(np.max(np.abs(b_rbj - bn)), np.max(np.abs(a_rbj - an)))
d_or = max(np.max(np.abs(b_or - bn)), np.max(np.abs(a_or - an)))
print(f"\n  |RBJ − scipy|       = {d_rbj:.3e}  {'✓' if d_rbj < 1e-12 else '✗'}")
print(f"  |Orfanidis − scipy| = {d_or:.3e}  {'✓ 就是这套' if d_or < 1e-12 else '✗'}")
print(f"  全式：gain = 1/(1+β), β = tan(w0/(2Q)), w0 = 2π·f0/fs")
print(f"        b = gain·[1, −2cos w0, 1]")
print(f"        a = [1, −2·gain·cos w0, 2·gain − 1]")

zpk = signal.tf2zpk(bn, an)
print(f"  验证零点：{np.abs(zpk[0])}（模=1 → 正好落在单位圆上，对应 {F0:g} Hz）")

# ------------------------------------------------------------ 4) 频响验证
print("\n" + "-" * 76)
print("[4] 用这些系数反算频响，确认它真的构成 20–450 Hz 带通 + 50 Hz 陷波")
print("-" * 76)
w, h = signal.sosfreqz(sos, worN=40021, fs=FS)
mag_db = 20 * np.log10(np.abs(h) + 1e-300)


def at(f):
    return mag_db[np.argmin(np.abs(w - f))]


for f in (2, 5, 20, 50, 100, 200, 300, 450, 490):
    print(f"    |H| @{f:>4} Hz = {at(f):+8.2f} dB")
band = (w > 5) & (w < 60)
lo = w[band][np.argmin(np.abs(mag_db[band] + 3.0103))]
band2 = (w > 250) & (w < 495)
hi = w[band2][np.argmin(np.abs(mag_db[band2] + 3.0103))]
print(f"    实测 −3 dB 点：低 {lo:.2f} Hz   高 {hi:.2f} Hz   （设定 {HP:g} / {LP:g}）")
pb = mag_db[(w > 60) & (w < 400)]
print(f"    通带 {60:g}–{400:g} Hz 内最大/最小：{pb.max():+.3f} / {pb.min():+.3f} dB"
      f"（Butterworth 无纹波）")
print(f"    50 Hz 陷波深度：{at(50):+.2f} dB")

print("\n" + "=" * 76)
print("结论：b0 等系数不是手填或拟合的，而是 scipy 按上面这条解析链算出来的；")
print("      每一步都能独立复现，且与实测频响一致。")
print("=" * 76)
