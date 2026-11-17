"""Note de qualite d'une alerte : A, B ou C.

Le score (0 a 100) dit si les conditions techniques de la cassure sont
reunies. La note de qualite regarde autre chose : ce qui entoure la
cassure. Cinq questions, chacune favorable, defavorable ou neutre :

  1. Le mouvement est-il confirme ailleurs (autres plateformes, ou
     acheteurs on-chain pour un jeton Alpha) ?
  2. Le marche dans son ensemble monte-t-il ou baisse-t-il ?
  3. Les acheteurs dominent-ils depuis plusieurs heures, ou seulement
     sur la derniere bougie ?
  4. Le stop est-il a une distance raisonnable ?
  5. Le depart est-il recent, ou une bonne partie du mouvement est-elle
     deja faite ?

A : au moins 4 points favorables et aucun defavorable.
C : 1 point favorable au plus, ou 3 defavorables et plus.
B : le reste.

Ces seuils sont un tri de bon sens, pas une regle prouvee : la note est
enregistree avec chaque alerte, et le bilan montre si les A font
reellement mieux que les C.

Auteur : Aziz Mernissi
"""

from __future__ import annotations

from typing import List, Optional

RANK = {"A": 0, "B": 1, "C": 2}


def grade(risk_pct: Optional[float], run_pct: Optional[float], market_change: Optional[float],
          confirmations: Optional[List[dict]] = None, cvd: Optional[float] = None,
          onchain: Optional[dict] = None) -> dict:
    pros: List[str] = []
    cons: List[str] = []
    known = 0

    def check(state: int, text: str) -> None:
        nonlocal known
        known += 1
        if state > 0:
            pros.append(text)
        elif state < 0:
            cons.append(text)

    # 1. Confirmation en dehors de Binance.
    if onchain:
        share = onchain.get("buyer_share")
        pct = f" ({share * 100:.0f} % d'acheteurs)" if share is not None else ""
        check({"confirme": 1, "contredit": -1}.get(onchain.get("verdict"), 0),
              {"confirme": f"acheteurs dominants on-chain{pct}",
               "contredit": f"vendeurs dominants on-chain{pct}"}.get(onchain.get("verdict"), ""))
    elif confirmations:
        seen = [c for c in confirmations if c.get("ratio") is not None]
        if seen:
            ok = sum(1 for c in seen if c.get("confirmed"))
            check(1 if ok else -1,
                  f"volume confirme sur {ok} autre(s) plateforme(s) sur {len(seen)}" if ok
                  else "mouvement visible sur Binance seulement")
    # 2. Sens du marche.
    if market_change is not None:
        check(1 if market_change >= 0 else (-1 if market_change <= -2 else 0),
              f"marche en hausse sur 24 h ({market_change:+.1f} %)" if market_change >= 0
              else f"marche en baisse sur 24 h ({market_change:+.1f} %)")
    # 3. Acheteurs sur la duree.
    if cvd is not None:
        check(1 if cvd >= 0.10 else (-1 if cvd <= -0.05 else 0),
              f"acheteurs dominants depuis plusieurs heures (delta {cvd * 100:+.0f} %)" if cvd >= 0.10
              else f"vendeurs dominants avant la cassure (delta {cvd * 100:+.0f} %)")
    # 4. Distance du stop.
    if risk_pct is not None:
        if risk_pct < 2:
            check(-1, f"stop tres serre (-{risk_pct:.1f} %) : une meche peut le toucher")
        elif risk_pct > 8:
            check(-1, f"stop tres eloigne (-{risk_pct:.1f} %)")
        else:
            check(1 if risk_pct >= 2.5 else 0, f"stop a distance raisonnable (-{risk_pct:.1f} %)")
    # 5. Avancement du mouvement.
    if run_pct is not None:
        check(1 if run_pct < 5 else (-1 if run_pct > 10 else 0),
              f"depart recent ({run_pct:+.1f} %)" if run_pct < 5
              else f"deja {run_pct:+.1f} % : une partie du mouvement est faite")

    if len(pros) >= 4 and not cons:
        letter = "A"
    elif len(pros) <= 1 or len(cons) >= 3:
        letter = "C"
    else:
        letter = "B"
    return {"grade": letter, "fav": len(pros), "con": len(cons), "known": known,
            "pros": pros, "cons": cons}


def lines(q: dict) -> List[str]:
    out = [f"Qualite : {q['grade']} - {q['fav']} point(s) pour, {q['con']} contre, sur {q['known']}"]
    if q["pros"]:
        out.append("  Pour : " + " ; ".join(q["pros"]))
    if q["cons"]:
        out.append("  Contre : " + " ; ".join(q["cons"]))
    return out


def allowed(letter: str, minimum: str) -> bool:
    """Vrai si la note atteint le minimum demande (A est la meilleure)."""
    return RANK.get(letter, 2) <= RANK.get(str(minimum).upper(), 2)
