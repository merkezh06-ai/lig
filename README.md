# 
Dosya seçilmedi
Kitaplık
Ara

Yeni

Tümü

Görseller

Belgeler




Ad
Değiştirildi
Boyut

Kitaplık
/
futbol_analiz_merkezi.html
1
2
3
4
5
6
7
8
9
10
11
12
13
14
15
16
17
18
19
20
21
22
23
24
25
26
27
28
29
30
31
32
33
34
35
36
37
38
39
40
41
42
43
44
45
46
47
48
49
50
51
52
53
54
55
56
57
58
59
60
61
62
63
64
65
66
67
68
69
70
<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Futbol Analiz Merkezi</title>
<meta name="description" content="6 lig için futbol maç analiz, oran hareketi ve tahmin dashboard'u">
<style>
:root{
  --bg:#0a0d12;--panel:#11161e;--panel2:#151b24;--line:#252d39;
  --text:#eef2f7;--muted:#8e99a8;--green:#28d17c;--red:#ff5d6c;
  --yellow:#f5c451;--blue:#5aa7ff;--cyan:#49d7e8;
  --radius:16px;--shadow:0 12px 35px rgba(0,0,0,.25);
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif}
button,input,select{font:inherit}
button{cursor:pointer}
.app{max-width:1500px;margin:auto;padding:22px}
.topbar{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:18px}
.brand{display:flex;align-items:center;gap:12px}
.logo{width:44px;height:44px;border-radius:13px;background:linear-gradient(135deg,#2bd47d,#1589ff);display:grid;place-items:center;font-weight:900;color:#07100c}
.brand h1{font-size:21px;margin:0}.brand p{margin:3px 0 0;color:var(--muted);font-size:12px}
.status{display:flex;gap:8px;flex-wrap:wrap}
.pill{border:1px solid var(--line);background:var(--panel);border-radius:999px;padding:8px 11px;font-size:12px;color:var(--muted)}
.pill.live{color:var(--green);border-color:rgba(40,209,124,.25)}
.controls{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:14px;display:grid;grid-template-columns:1fr auto auto;gap:12px;margin-bottom:16px}
.leagues{display:flex;gap:7px;flex-wrap:wrap}
.league{background:var(--panel2);border:1px solid var(--line);color:var(--muted);border-radius:10px;padding:9px 12px;font-weight:700;font-size:12px}
.league.active{color:#07100c;background:var(--green);border-color:var(--green)}
.control{background:var(--panel2);border:1px solid var(--line);color:var(--text);border-radius:10px;padding:9px 11px;outline:none}
.hero{display:grid;grid-template-columns:1.35fr .65fr;gap:16px;margin-bottom:16px}
.heroCard,.panel{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow)}
.heroCard{padding:20px}
.eyebrow{color:var(--green);font-size:11px;font-weight:900;letter-spacing:.12em;text-transform:uppercase}
.heroTitle{font-size:28px;line-height:1.1;margin:8px 0}
.heroText{color:var(--muted);font-size:13px;max-width:720px;line-height:1.6}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:18px}
.stat{background:var(--panel2);border:1px solid var(--line);border-radius:12px;padding:12px}
.stat b{display:block;font-size:21px}.stat span{font-size:11px;color:var(--muted)}
.modelCard{padding:20px;display:flex;flex-direction:column;justify-content:space-between}
.scoreRing{width:118px;height:118px;border-radius:50%;display:grid;place-items:center;margin:auto;background:conic-gradient(var(--green) 0 87%,#27303b 87% 100%)}
.scoreRing>div{width:88px;height:88px;border-radius:50%;background:var(--panel);display:grid;place-items:center;text-align:center}
.scoreRing strong{font-size:25px}.scoreRing small{display:block;color:var(--muted);font-size:10px}
.sectionHead{display:flex;justify-content:space-between;align-items:end;gap:12px;margin:20px 0 10px}
.sectionHead h2{font-size:17px;margin:0}.sectionHead p{margin:4px 0 0;color:var(--muted);font-size:12px}
.grid5{display:grid;grid-template-columns:repeat(5,1fr);gap:10px}
.pick{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px;transition:.15s}
.pick:hover{border-color:#3b4655;transform:translateY(-1px)}
.pickTop{display:flex;justify-content:space-between;gap:8px;color:var(--muted);font-size:10px}
.pickTeams{font-weight:800;font-size:14px;margin:12px 0 8px;line-height:1.35}
.pickMain{display:flex;justify-content:space-between;align-items:end;gap:8px}
.pickPred{font-size:12px;color:var(--muted)}.pickPred b{display:block;color:var(--green);font-size:18px}
.conf{font-weight:900;color:var(--green);font-size:14px}
.badge{display:inline-flex;align-items:center;gap:5px;border-radius:999px;padding:4px 7px;font-size:10px;background:rgba(40,209,124,.1);color:var(--green)}
.featureGrid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
.feature{padding:15px;background:var(--panel);border:1px solid var(--line);border-radius:14px}
.feature span{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.08em}
.feature strong{display:block;margin-top:7px;font-size:14px}
.feature em{font-style:normal;color:var(--green);font-weight:800;font-size:12px}
.matches{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}
.match{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:15px}
.matchHead{display:flex;justify-content:space-between;color:var(--muted);font-size:11px}
.matchBody{display:grid;grid-template-columns:1fr auto 1fr;align-items:center;gap:12px;margin:16px 0}
.team{text-align:center;font-weight:850;font-size:15px}.team small{display:block;color:var(--muted);font-weight:500;font-size:10px;margin-top:4px}
.vs{text-align:center;color:var(--muted);font-size:11px}
.probs{display:grid;grid-template-columns:repeat(3,1fr);gap:6px}
.prob{background:var(--panel2);border-radius:9px;padding:8px;text-align:center}.prob small{display:block;color:var(--muted);font-size:9px}.prob b{font-size:14px}
.green{color:var(--green)!important}.red{color:var(--red)!important}.yellow{color:var(--yellow)!important}

