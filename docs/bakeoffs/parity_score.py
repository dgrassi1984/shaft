"""Parity score for docs/bakeoffs/leaderboard.md: each model's Elo against what its cost and speed predict.

Run `python3 docs/bakeoffs/parity_score.py`. Stdlib only, fixed seed, writes nothing. DATA holds the published per-run
figures: seconds and dollars per cycle, and W-L-T against that bake-off's baseline. CLAIMS holds each model's supported
claims. The weighted score is parity minus LAMBDA times the share of unsupported claims: the wins already carry the
judge's penalty for a false claim in that cycle, and LAMBDA prices what they cannot see, a false report misleading later
cycles and the operator. It is a judgement call, not a fit.
"""
import math
import random

SEED = 20261105
B = 3000
PRIOR_SD = 2.0
NAMES = ["MiMo", "DeepSeek", "M3.1Flash", "Nemotron", "HY3",
         "M3", "Step", "GLM", "Sonnet", "HaikuMed", "HaikuXhigh",
         "Argon", "Sol", "Terra",
         "Grok4.7", "GLM5.3", "GLM5.3Flash", "Hy4", "KimiK3.1", "Particle1", "Mercury3",
         "SolCodex", "AstraCodex", "LunaCodex"]
# bake-off, baseline, [(model, seconds, dollars, W, L, T)]
DATA = [
 ("big", "MiMo", [("MiMo",670,.0580,0,0,0),
                  ("DeepSeek",193,.0305,16,15,9)]),
 ("screen", "MiMo", [("MiMo",826,.0598,0,0,0),
  ("M3.1Flash",211,.0389,4,6,2), ("Nemotron",79,.0369,0,11,1),
  ("HY3",318,.0342,4,5,3), ("M3",413,.1521,9,3,0),
  ("Step",217,.0476,2,9,1), ("GLM",299,.0888,0,11,1)]),
 ("sonnet", "MiMo", [("MiMo",561,.0437,0,0,0),
                     ("Sonnet",34,.2835,0,10,2)]),
 ("F", "MiMo", [("MiMo",640,.0537,0,0,0),
  ("HaikuMed",53,.0203,1,11,0), ("HaikuXhigh",154,.0499,3,7,2),
  ("DeepSeek",211,.0307,6,5,1)]),
 ("or5", "MiMo", [("MiMo",570,.0396,0,0,0),
  ("Argon",192,.3253,9,2,1), ("HaikuMed",62,.0144,1,8,3),
  ("HaikuXhigh",150,.0486,8,4,0)]),
 ("dshx40", "DeepSeek", [("DeepSeek",193,.0233,0,0,0),
                         ("HaikuXhigh",151,.0525,15,20,5)]),
 ("gpt61", "DeepSeek", [("DeepSeek",328,.0564,0,0,0),
  ("Sol",264,.5704,27,10,3), ("Terra",287,.4858,15,18,6)]),
 # Candidates' 11-05 runs judged against DeepSeek's stored runs of the same 12 cycles.
 # Codex arms: fixed prompt, dollars are list-price estimates (the plan bills nothing per call).
 ("bridge", "DeepSeek", [("DeepSeek",296,.0498,0,0,0),
  ("Grok4.7",400,.5752,3,8,1), ("GLM5.3",315,.1198,2,8,2),
  ("GLM5.3Flash",348,.0119,2,10,0), ("Hy4",794,.2107,6,4,2),
  ("KimiK3.1",575,.4148,5,5,2), ("Particle1",303,.0297,6,5,1),
  ("Mercury3",247,.0215,4,6,2),
  ("SolCodex",132,.1246,8,4,0), ("AstraCodex",200,.6297,8,4,0),
  ("LunaCodex",74,.0077,0,12,0)])
]
# Model: (claims supported, claims judged, distinct runs). Every judgment of a run counts, whoever it was paired with.
# Codex arms: only runs with the fixed prompt.
CLAIMS = {"MiMo":(354,386,87), "HaikuMed":(41,46,23), "HaikuXhigh":(116,126,63), "DeepSeek":(463,581,132),
          "M3.1Flash":(18,24,12), "Nemotron":(11,24,12), "HY3":(22,24,12), "M3":(23,24,12), "Step":(20,24,12),
          "GLM":(18,24,12), "Sonnet":(22,24,12), "Argon":(22,22,11), "Sol":(80,80,40), "Terra":(78,79,40),
          "Grok4.7":(32,48,12), "GLM5.3":(28,48,12), "GLM5.3Flash":(19,48,12), "Hy4":(46,48,12),
          "KimiK3.1":(32,48,12), "Particle1":(27,48,12), "Mercury3":(32,44,11), "SolCodex":(24,24,12),
          "AstraCodex":(24,24,12), "LunaCodex":(18,24,12)}
LAMBDA = 100   # Elo points for 100% unsupported claims: one point per percentage point.
K = len(NAMES)
INDEX = {name:i for i,name in enumerate(NAMES)}
ELO = 400 / math.log(10)

def solve(a, b):
    a = [list(row) + [v] for row,v in zip(a,b)]
    n = len(b)
    for j in range(n):
        p = max(range(j,n), key=lambda i:abs(a[i][j]))
        a[j],a[p] = a[p],a[j]
        if abs(a[j][j]) < 1e-12:
            raise ValueError("Singular design")
        for i in range(j+1,n):
            f = a[i][j] / a[j][j]
            for t in range(j+1,n+1):
                a[i][t] -= f*a[j][t]
    x = [0.0]*n
    for j in range(n-1,-1,-1):
        x[j] = (a[j][n]-sum(a[j][t]*x[t] for t in range(j+1,n)))/a[j][j]
    return x

def ols(x,y):
    p = len(x[0])
    a = [[sum(r[i]*r[j] for r in x) for j in range(p)] for i in range(p)]
    b = [sum(r[i]*v for r,v in zip(x,y)) for i in range(p)]
    return solve(a,b)

def edges(data):
    return [(INDEX[m],INDEX[base],(w,l,t))
            for _,base,arms in data for m,_,_,w,l,t in arms if m != base]

def bt(ed,sd=PRIOR_SD):
    # MiMo is fixed at zero. The Gaussian prior penalizes centered
    # abilities, so the prior treats every model symmetrically.
    d = K-1
    theta = [0.0]*d
    lam = 1/(sd*sd)
    for _ in range(60):
        total = sum(theta)
        g = [-lam*(v-total/K) for v in theta]
        h = [[lam*((1.0 if i==j else 0.0)-1/K)
              for j in range(d)] for i in range(d)]
        for i,j,(w,l,t) in ed:
            delta = (theta[i-1] if i else 0)-(theta[j-1] if j else 0)
            p = 1/(1+math.exp(-delta))
            n = w+l+t
            err = w+.5*t-n*p
            curv = n*p*(1-p)
            for u,sign in ((i,1),(j,-1)):
                if not u:
                    continue
                g[u-1] += sign*err
                h[u-1][u-1] += curv
            if i and j:
                h[i-1][j-1] -= curv
                h[j-1][i-1] -= curv
        step = solve(h,g)
        theta = [a+b for a,b in zip(theta,step)]
        if max(map(abs,step)) < 1e-9:
            return [0.0]+[v*ELO for v in theta]
    raise RuntimeError("BT failed to converge")

def indices(data):
    # Unweighted OLS: one observation per arm/bake-off.
    # All bake-off intercepts included; MiMo model effect omitted.
    p = K-1+len(data)
    x, yc, yt = [],[],[]
    for b,(_,_,arms) in enumerate(data):
        for m,s,c,_,_,_ in arms:
            row = [0.0]*p
            if INDEX[m]:
                row[INDEX[m]-1] = 1
            row[K-1+b] = 1
            x.append(row)
            yc.append(math.log(c))
            yt.append(math.log(s))
    c,t = ols(x,yc),ols(x,yt)
    return [0.0]+c[:K-1], [0.0]+t[:K-1]

def parity(y,c,t,cost_only=False,omit=None):
    x = [[1,ci] if cost_only else [1,ci,ti] for ci,ti in zip(c,t)]
    ids = [i for i in range(K) if i != omit]
    b = ols([x[i] for i in ids],[y[i] for i in ids])
    r = [v-sum(a*z for a,z in zip(row,b)) for row,v in zip(x,y)]
    return b,r

def quant(v,p):
    v = sorted(v)
    h = (len(v)-1)*p
    i = int(h)
    return v[i]+(v[min(i+1,len(v)-1)]-v[i])*(h-i)

def interval(v):
    return "[%.2f, %.2f]" % (quant(v,.05),quant(v,.95))

def order(r):
    return sorted(range(K),key=lambda i:(-r[i],i))

def names_order(r):
    return " > ".join(NAMES[i] for i in order(r))

def unsupported():
    return [1-CLAIMS[n][0]/CLAIMS[n][1] for n in NAMES]

def weighted(r,u,lam=LAMBDA):
    return [a-lam*b for a,b in zip(r,u)]

def weighted_report(r,rs):
    u = unsupported()
    w = weighted(r,u)
    rng = random.Random(SEED+1)
    ws,ranks = [],[]
    for rr in rs:   # a binomial draw of the unsupported share on the model's distinct runs
        du = [sum(rng.random()<u[i] for _ in range(CLAIMS[n][2]))/CLAIMS[n][2] for i,n in enumerate(NAMES)]
        d = weighted(rr,du)
        ws.append(d)
        rank = [0]*K
        for pos,i in enumerate(order(d),1):
            rank[i] = pos
        ranks.append(rank)
    print("Weighted = parity - %d * unsupported share" % LAMBDA)
    print("rank model weighted 90%CI parity supported/judged unsupported rank90% top3_frequency")
    for pos,i in enumerate(order(w),1):
        print("%d %s %.2f %s %.2f %d/%d %.3f %.0f-%.0f %.3f" %
              (pos,NAMES[i],w[i],interval([z[i] for z in ws]),r[i],CLAIMS[NAMES[i]][0],CLAIMS[NAMES[i]][1],u[i],
               quant([z[i] for z in ranks],.05),quant([z[i] for z in ranks],.95),
               sum(z[i]<=3 for z in ranks)/B))
    for lam in (0,50,100,200,350):
        print("Lambda %d; %s" % (lam,names_order(weighted(r,u,lam))))

def run():
    ed = edges(DATA)
    c,t = indices(DATA)
    y = bt(ed)
    b,r = parity(y,c,t)
    rng = random.Random(SEED)
    ys,rs,bs,ranks = [],[],[],[]
    for _ in range(B):
        draw = []
        for i,j,(w,l,tie) in ed:
            n = w+l+tie
            counts = [0,0,0]
            for z in range(n):
                u = rng.random()*n
                counts[0 if u<w else 1 if u<w+l else 2] += 1
            draw.append((i,j,counts))
        yy = bt(draw)
        bb,rr = parity(yy,c,t)
        ys.append(yy)
        rs.append(rr)
        bs.append([bb[1]*math.log(2),bb[2]*math.log(2)])
        rank = [0]*K
        for pos,i in enumerate(order(rr),1):
            rank[i] = pos
        ranks.append(rank)
    print("Seed=%d bootstrap=%d models=%d pairs=%d judged_pairs=%d" %
          (SEED,B,K,len(ed),sum(sum(v) for _,_,v in ed)))
    print("Prior SD logits=%.1f; Elo=%.1f" % (PRIOR_SD,PRIOR_SD*ELO))
    print("rank model Elo 90%%CI cost_index time_index parity 90%%CI rank90%% top3_frequency")
    for pos,i in enumerate(order(r),1):
        print("%d %s %.2f %s %.3f %.3f %.2f %s %.0f-%.0f %.3f" %
          (pos,NAMES[i],y[i],interval([z[i] for z in ys]),
           math.exp(c[i]),math.exp(t[i]),r[i],interval([z[i] for z in rs]),
           quant([z[i] for z in ranks],.05),quant([z[i] for z in ranks],.95),
           sum(z[i]<=3 for z in ranks)/B))
    print("Intercept %.1f" % b[0])
    print("Doubling cost %.1f %s; time %.1f %s" %
          (b[1]*math.log(2),interval([z[0] for z in bs]),
           b[2]*math.log(2),interval([z[1] for z in bs])))
    mean = sum(y)/K
    sst = sum((v-mean)**2 for v in y)
    print("R2 %.3f" % (1-sum(v*v for v in r)/sst))
    nf = [v for v in DATA if v[0]!="F"]
    cn,tn = indices(nf)
    yn = bt(edges(nf))
    bn,rn = parity(yn,cn,tn)
    bc,rc = parity(y,c,t,cost_only=True)
    print("No F doubling cost %.1f time %.1f; %s" %
          (bn[1]*math.log(2),bn[2]*math.log(2),names_order(rn)))
    print("Cost only doubling cost %.1f; %s" %
          (bc[1]*math.log(2),names_order(rc)))
    weighted_report(r,rs)
    print("Sensitivity per model: full_rank noF_rank costOnly_rank")
    for i,name in enumerate(NAMES):
        print(name,order(r).index(i)+1,order(rn).index(i)+1,order(rc).index(i)+1)
    print("Leave one model out: omitted cost_doubling time_doubling")
    for i,name in enumerate(NAMES):
        bb,_ = parity(y,c,t,omit=i)
        print("%s %.1f %.1f" % (name,bb[1]*math.log(2),bb[2]*math.log(2)))
    for sd in (1.0,4.0):
        yy = bt(ed,sd)
        bb,rr = parity(yy,c,t)
        print("Prior SD %.1f doubling cost %.1f time %.1f; %s" %
              (sd,bb[1]*math.log(2),bb[2]*math.log(2),names_order(rr)))
    print("Adjacent ordering probabilities in full ranking:")
    oo = order(r)
    for i,j in zip(oo,oo[1:]):
        print("%s > %s %.3f" %
              (NAMES[i],NAMES[j],sum(z[i]>z[j] for z in rs)/B))

if __name__ == "__main__":
    run()
