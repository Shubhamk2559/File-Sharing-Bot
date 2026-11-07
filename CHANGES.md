# Smart rotation upgrade: kya badla hai

Har file ko apne project mein **same path** par replace/add karo (folder structure zip ke andar wahi hai).

## NEW files (add karo)
| File | Kya hai |
|---|---|
| `ads/bandit.py` | Naya engine: Thompson sampling, time-decay, value estimate, probation, traffic-share simulation |
| `tests/test_bandit.py` | 26 naye tests (bandit maths, validation, tick/back-off, report) |

## CHANGED files (replace karo)
| File | Kya badla |
|---|---|
| `ads/settings.py` | Naya `strategy` (`thompson` default / `weighted`), har network ka `ecpm_usd`, naye optimization knobs: `half_life_days`, `prior_strength`, `min_share`, `probation_share`, `use_monetag_estimate`, aur unki validation |
| `ads/engine.py` | `blockers()` (traffic kyun nahi mil raha), Thompson-based `plan_request`, naya `build_model`, naya `tick`: bench back-off x2/x4/x8, probation, strike forgiveness, plan doc mein model/shares/probation save, revenue join fast |
| `ads/report.py` | Report mein `earnings` (expected per 1000 requests, uplift vs equal split), har network ka `expected`, `blockers_text`, `hints`; Telegram summary mein hints + expected earnings |
| `ads/admin_ads.html` | Naya "Earnings engine" panel, strategy select, per-network "Your eCPM" input, naye optimization fields + Monetag estimate checkbox |
| `README.md` | Rotation section naya, "Rotation does nothing" checklist, tests ka note |

## Baaki sab kuch same hai
`main.py`, `ads/routes.py`, `ads/scoring.py`, `ads/service.py`, `ads/store.py`, `ads/verify.py`, adapters, DB collections: koi change nahi. Naya DB collection ya migration nahi chahiye; naye fields `plan` document aur config mein apne aap bhar jaate hain.

## Update ke baad
1. Redeploy / restart karo. Optimizer pehli baar ~5 min (`ADS_OPT_INTERVAL`) mein model banata hai; tab tak purana cold-start rotation chalega.
2. `/admin/ads` kholo: **Earnings engine** panel dekho, jo network "no" dikha raha hai uski wajah wahin likhi hai.
3. Kam se kam 2 networks enable karo aur har ek ka **eCPM** ya daily **revenue** daalo.
4. Purana behaviour chahiye to Routing mein strategy ko "Classic weighted score" kar do.
