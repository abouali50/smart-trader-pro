# Smart Trader Pro

Scanner de cryptomonnaies, avec cinq moteurs :

- **Signal Binance** : toutes les 15 minutes, analyse toutes les paires USDT
  de Binance sur des bougies de 15 minutes et repère celles qui explosent,
  c'est-à-dire qui cassent le plus haut des dernières 24 heures avec un
  volume anormal et des acheteurs agressifs. C'est le signal mis en avant sur
  la page et sur Telegram.
- **Pré-explosion** : repère les cryptos sous pression avant qu'elles cassent
  (prix calme, acheteurs qui accumulent sous une résistance), sur Binance et
  sur Binance Alpha.
- **Binance Alpha** : applique le même détecteur d'explosion aux jetons de la
  rubrique Alpha de Binance.
- **Marché large** : balaie 4000 cryptos via CoinGecko et signale les petites
  qui se réveillent.
- **Replis en tendance** : sur les paires les plus liquides, attend un repli
  propre dans une tendance haussière.

Chaque configuration est notée sur 100 ; une alerte part quand plusieurs
familles d'indicateurs s'accordent.

**Il analyse, il ne trade pas.** Aucune clé d'échange n'est demandée, aucun
ordre n'est passé. La décision reste la tienne.

Auteur : Aziz Mernissi

## Installation sur le VPS (copier-coller)

Connecte-toi en SSH au serveur, puis colle cette ligne :

```bash
curl -fsSL https://raw.githubusercontent.com/abouali50/smart-trader-pro/main/deploy/install.sh | sudo bash
```

Le script installe Python, télécharge le code dans `/opt/smart-trader-pro`, crée
un service qui redémarre tout seul, et lance la surveillance. Relancer la même
ligne met à jour le scanner sans toucher à tes réglages.

Voir ce qu'il fait :

```bash
journalctl -u smart-trader-pro -f
```

## Tableau de bord web

Après l'installation, ouvre dans ton navigateur (ordinateur ou téléphone) :

```
http://ADRESSE_DU_SERVEUR:8765
```

La page montre le contexte du marché, les paires surveillées avec leur score
et les derniers signaux. Touche une ligne pour voir le détail des conditions.
Elle se met à jour toute seule chaque minute.

La page est en lecture seule : elle n'accepte aucune saisie et ne sert que
le tableau. Elle est toutefois visible par quiconque connaît l'adresse.
Pour changer de port, renseigne `DASHBOARD_PORT` dans `.env`.

### Avec un nom de domaine et HTTPS

Si le serveur utilise déjà Caddy, crée d'abord un enregistrement DNS de type
`A` pointant vers l'adresse du serveur, puis lance :

```bash
curl -fsSL https://raw.githubusercontent.com/abouali50/smart-trader-pro/main/deploy/domain.sh | sudo bash -s -- trader.exemple.com
```

Le script ajoute un bloc à la fin du Caddyfile après en avoir fait une
sauvegarde, et restaure celle-ci si Caddy refuse la configuration. Une fois
le site en ligne en HTTPS, l'accès direct par le port est fermé.

## Recevoir les signaux sur Telegram

1. Dans Telegram, écris à `@BotFather`, envoie `/newbot` et garde le jeton.
2. Écris un message à ton nouveau bot, puis ouvre
   `https://api.telegram.org/bot<JETON>/getUpdates` pour lire ton `chat id`.
3. Sur le VPS :

```bash
sudo nano /opt/smart-trader-pro/.env
sudo systemctl restart smart-trader-pro
cd /opt/smart-trader-pro && sudo -u smarttrader bash -c 'set -a; . ./.env; .venv/bin/python -m smart_trader_pro test-telegram'
```

## CoinGecko et CoinMarketCap

Ces deux sources donnent le contexte du marché ; elles ne déclenchent pas de
signal à elles seules.

| Source | Clé | Apporte |
| --- | --- | --- |
| CoinGecko | facultative (`COINGECKO_API_KEY`) | variation du marché global sur 24 h, dominance du BTC, cryptos en tendance |
| CoinMarketCap | obligatoire (`CMC_API_KEY`, gratuite sur pro.coinmarketcap.com) | dominance du BTC, indice Peur et Avidité |

Si le marché global perd plus de `market_drop_block_pct` % en 24 h, les
signaux sont suspendus. Une source en panne est ignorée, le scanner continue.

## Le marché large : 4000 cryptos

Binance ne cote que quelques centaines de cryptos. Toutes les 15 minutes, le
scanner balaie les 4000 premières par capitalisation via CoinGecko et cherche
celles qui se réveillent : volume très supérieur à leurs habitudes et prix en
hausse sur l'heure, sans être déjà envolées.

- Une crypto doit échanger au moins `wide_min_volume` dollars par jour
  (300 000 par défaut) pour être retenue : en dessous, on ne peut ni entrer
  ni sortir proprement.
- Le scanner apprend d'abord les habitudes de chaque crypto : il lui faut
  environ une heure avant la première alerte.
- Chaque alerte « RÉVEIL » indique où la crypto se négocie.
- **Bilan automatique** : chaque réveil signalé est suivi, et son résultat est
  figé 24 h plus tard. La page affiche la moyenne et la part de réveils en
  hausse. Cette méthode n'a pas pu être testée sur le passé ; ce bilan est
  la seule preuve de ce qu'elle vaut.

Limites : CoinGecko fournit un volume cumulé sur 24 h, donc un réveil est vu
avec un léger retard, et aucun stop n'est calculé. Les petites cryptos sont
peu liquides et souvent manipulées.

Sans clé, le balayage utilise l'accès public de CoinGecko, lent et parfois
limité. Une clé payante (`COINGECKO_PRO_API_KEY` dans `.env`) le rend rapide
et fiable.

## Le signal Binance

Un mouvement violent commence presque toujours par une cassure accompagnée
d'un volume très supérieur à l'habitude. Le signal cherche ce moment sur des
bougies de 15 minutes, pour être prévenu dans le quart d'heure.

| Famille | Points | Ce qui est mesuré |
| --- | --- | --- |
| Volume | 30 | volume de la bougie contre sa moyenne, volume des 4 dernières bougies, OBV au plus haut |
| Acheteurs | 10 | part du volume achetée au prix du marché (ordres agressifs) |
| Cassure | 30 | clôture au-dessus du plus haut des 96 dernières bougies (24 h), sortie de compression de Bollinger |
| Momentum | 15 | RSI, MACD, ADX, clôture près du plus haut |
| Force relative | 10 | performance face au Bitcoin sur 24 h |
| Risque | 5 | le départ est encore récent (moins de 15 % déjà parcourus en 24 h) |

Une alerte « EXPLOSION BINANCE » exige une cassure **et** un score d'au moins
`radar_strategy.min_score` (70 par défaut). Elle donne l'entrée, le stop, deux
objectifs, les niveaux (VWAP, profil de volume, flux d'ordres) et les
actualités de la crypto. Sur la page, elle apparaît dans un encart orange en
haut.

**Filtre de marché.** Mesuré sur 40 paires en bougies de 15 minutes (août à
novembre 2026), ce signal était à peu près à l'équilibre tant que le marché
tenait, puis nettement perdant pendant une baisse de 30 % : aucune des 36
variantes n'a été gagnante sur la validation. Depuis la version 1.7, l'alerte
ne part donc que si le Bitcoin est en tendance haussière : il clôture
au-dessus de sa moyenne exponentielle sur `radar_strategy.market_bars` bougies
(672 = 7 jours) et cette moyenne monte. Quand le Bitcoin baisse, la page
affiche « Alertes suspendues » et les cassures restent visibles dans le
tableau, sans message Telegram. Le filtre vaut aussi pour Binance Alpha.
`"market_bars": 0` le coupe. Ce filtre a été choisi après avoir vu la période
de test : seules les semaines à venir diront s'il aide vraiment.

Il ne prédit pas l'avenir : il détecte un départ, et beaucoup de cassures
échouent. Avant de s'y fier, le mesurer sur l'historique :

```bash
sudo -u smarttrader .venv/bin/python -m smart_trader_pro compare --mode radar --count 40
```

La commande utilise les bougies de `radar_interval` ; `--interval 1h` permet
d'essayer une autre unité de temps.

## La pré-explosion : être prévenu avant

Dans un départ, le volume et les acheteurs bougent avant le prix. Pendant la
phase d'accumulation, le prix reste dans une zone étroite pendant que des
acheteurs absorbent ce qui se vend. Le scanner cherche cette signature sur
les mêmes bougies de 15 minutes :

| Famille | Points | Ce qui est mesuré |
| --- | --- | --- |
| Compression | 25 | bandes de Bollinger au plus serré, volatilité sous sa moyenne |
| Accumulation | 30 | delta cumulé positif, OBV en hausse alors que le prix est calme, acheteurs majoritaires |
| Volume | 15 | volume récent supérieur à l'habitude sans que le prix parte |
| Structure | 20 | prix à moins de 3 % sous sa résistance, creux de plus en plus hauts |
| Force | 10 | tient mieux que le Bitcoin sur 24 h |

Une alerte « ⏳ PRÉ-EXPLOSION » exige un score d'au moins
`pressure_strategy.min_score` (75 par défaut), un prix encore sous la
résistance et moins de 10 % de hausse déjà faite. Elle donne le niveau à
casser et deux façons d'agir : attendre la clôture au-dessus du niveau, ou
entrer en avance avec un stop sous la zone. Trois alertes au plus par passage.

Ce n'est pas une prédiction : une zone calme peut ne jamais casser, ou casser
vers le bas. Mesurée sur 40 paires en bougies de 15 minutes (août à novembre
2026), cette approche a perdu de l'argent sur la période de validation, dans
un marché en baisse : aucune des 24 variantes n'a été gagnante. Les alertes
restent donc sur la page comme liste d'observation et ne partent pas sur
Telegram, sauf si `pressure_telegram` vaut `true`. Pour refaire la mesure :

```bash
sudo -u smarttrader .venv/bin/python -m smart_trader_pro compare --mode pression --count 40
```

## Binance Alpha

La rubrique Alpha de Binance regroupe des jetons jeunes, négociés avant une
éventuelle cotation classique. Toutes les 15 minutes, le scanner lit la liste
des jetons Alpha et leurs bougies par l'API publique de Binance Alpha, puis
leur applique le détecteur d'explosion du signal Binance.

- Seuls les jetons qui échangent au moins `alpha_min_volume` dollars par jour
  (200 000 par défaut) avec au moins `alpha_min_liquidity` dollars de
  liquidité (50 000) sont analysés.
- L'alerte « EXPLOSION BINANCE ALPHA » ajoute la carte du jeton : chaîne,
  capitalisation, liquidité, nombre de détenteurs.
- Ces adresses sont servies par www.binance.com et peuvent être refusées
  depuis certains pays. La page l'indique alors, et le reste du scanner
  continue de fonctionner.

Les jetons Alpha sont les plus risqués de tout ce que le scanner surveille :
liquidité faible, chutes aussi violentes que les hausses. Le détecteur n'a
pas été mesuré sur leur historique.

## Niveaux et flux d'ordres

Pour chaque paire, la page et les alertes indiquent :

| Mesure | Ce qu'elle dit |
| --- | --- |
| VWAP (24 bougies) | prix moyen pondéré par le volume ; au-dessus, les acheteurs récents sont gagnants |
| VWAP ancré | le même calcul démarré au dernier creux : le prix de revient moyen depuis ce creux |
| Profil de volume | le POC (prix le plus échangé) et la zone de valeur (70 % du volume) sur 120 bougies |
| Flux d'ordres | la part du volume achetée au marché sur la dernière bougie, et le delta cumulé sur 24 bougies |
| Carnet d'ordres | la part des achats en attente à ±1 % du prix, mesurée en direct |

Le profil de volume est estimé à partir des bougies, pas de chaque transaction.
Le carnet d'ordres n'est lu que pour les signaux et les cinq premières lignes
du signal Binance. Ces mesures sont du contexte : elles n'entrent pas dans le score.

## Actualités et autres plateformes

- **Actualités** : les titres des dernières 24 h de CoinDesk, Cointelegraph et
  Decrypt sont affichés sur la page et rattachés aux cryptos qu'ils citent.
  Les titres d'une crypto sont joints à ses alertes.
- **Autres plateformes** : quand le signal Binance se déclenche, le scanner
  vérifie si le volume s'emballe aussi sur Coinbase, OKX et Gate.io.

Ces deux éléments sont du contexte : ils n'entrent pas dans le score, pour
que le backtest reste comparable. Une source en panne est ignorée.

## Comment le score des replis est calculé

| Famille | Points | Indicateurs |
| --- | --- | --- |
| Tendance | 30 | tendance 4 h (EMA50 et sa pente), EMA200, ADX avec +DI/-DI |
| Structure | 25 | repli sur l'EMA21, zone Fibonacci 0.382-0.618, médiane de Bollinger |
| Momentum | 20 | RSI, MACD, RSI stochastique |
| Volume | 20 | volume relatif, OBV, Chaikin Money Flow, MFI |
| Risque | 5 | prix pas trop éloigné de l'EMA21 (en ATR) |

Un signal exige une tendance de fond haussière **et** un score d'au moins
`min_score` (70 par défaut). Les familles sont plafonnées exprès : dix
indicateurs de momentum qui disent la même chose ne valent pas dix
confirmations.

Deux garde-fous s'ajoutent : pas de signal si la tendance de fond du BTC est
baissière, et pas deux alertes sur la même paire en moins de `cooldown_hours`.

Chaque alerte donne l'entrée, le stop (sous le dernier creux, entre 1 et 3
ATR), deux objectifs (1,5 R et 3 R), les niveaux de Fibonacci et la taille de
position pour ne risquer que `risk_pct` % du capital.

## Voie rapide : « DÉPART EN COURS »

Le signal d'explosion attend la clôture d'une bougie de 15 minutes, puis le
passage sur toutes les paires : l'alerte peut arriver un quart d'heure après
le début du mouvement, quand une partie de la hausse est déjà faite.

La voie rapide tourne dans un fil séparé. Une fois par minute, elle prend une
photo de tout le marché Binance en un seul appel et la compare à celle prise
trois minutes plus tôt. Elle alerte si le prix a pris au moins 2 %, si le
volume de ces minutes vaut au moins 8 fois le rythme moyen des 24 heures, et
si le prix est passé au-dessus du plus haut des 24 dernières heures. Le prix
d'entrée affiché est le prix au moment de l'alerte. Si la crypto a déjà pris
plus de 12 % sur 24 heures, aucune alerte ne part : trop tard.

Contrepartie : une alerte donnée sur une bougie non terminée arrive plus tôt,
donc avec plus de faux départs. Ces réglages n'ont pas pu être testés sur
l'historique (il faudrait des données à la minute pour toutes les paires).
Ces alertes ont leur propre ligne dans le bilan, « Départ en cours », qui
dira si la vitesse paie.

Les alertes d'explosion classiques indiquent maintenant le prix au moment de
l'envoi, et « TROP TARD » si l'objectif 1 est déjà dépassé.

## Note de qualité A, B ou C

Le score dit si la cassure est techniquement nette. La note de qualité
regarde ce qui l'entoure, en cinq questions : le mouvement est-il confirmé
ailleurs (autres plateformes, ou acheteurs on-chain pour un jeton Alpha), le
marché monte-t-il, les acheteurs dominent-ils depuis plusieurs heures, le
stop est-il à une distance raisonnable, le départ est-il récent. A : au moins
4 points favorables et aucun défavorable. C : 1 favorable au plus, ou 3
défavorables. B : le reste.

La note et ses raisons figurent en tête de chaque alerte Telegram et dans
l'écran Trader. Avec `"min_quality": "B"`, seules les alertes A et B partent
sur Telegram ; les C restent sur la page et dans le bilan. Ces seuils sont un
tri de bon sens, pas une règle prouvée : le bilan est détaillé par note, pour
voir si les A font réellement mieux que les C.

## Flux on-chain des jetons Binance Alpha

Pour les jetons Alpha dont le contrat est connu, le scanner lit leurs
principaux pools de liquidité sur les DEX (API on-chain de CoinGecko, avec la
clé payante `COINGECKO_PRO_API_KEY`) : nombre de portefeuilles acheteurs et
vendeurs sur une heure, volume échangé sur 15 minutes, 1 heure et 24 heures,
liquidité. C'est une information que les bougies ne contiennent pas.

Le verdict (« acheteurs dominants », « vendeurs dominants » ou « neutre »)
est ajouté à l'alerte Telegram et à l'écran Trader. Il n'empêche aucune
alerte de partir : il est enregistré avec chaque alerte pour que le bilan
montre, avec le temps, s'il aide vraiment. Il n'existe pas d'historique de
ces données pour le tester à l'avance.

Chaque jeton interrogé coûte un crédit CoinGecko : `onchain_per_pass` (8 par
défaut) plafonne le nombre de jetons par passage, soit environ 23 000 crédits
par mois, en plus des 46 000 du balayage large, sur les 100 000 du plan Basic.

## L'application web : comptes, abonnements, écran de trading

La page d'accueil du serveur est une application : Accueil, Signaux, Trader
(graphique en bougies, niveaux, calculateur de position), Bilan, Abonnement
et Compte. Elle s'installe sur iPhone (Safari, Partager, « Sur l'écran
d'accueil ») et sur Mac. L'ancienne page reste disponible à l'adresse
`/classique`.

**Compte administrateur.** À créer une fois, sur le serveur ; le mot de passe
est demandé au clavier sans s'afficher :

```bash
cd /opt/smart-trader-pro && sudo -u smarttrader .venv/bin/python -m smart_trader_pro admin
```

L'administrateur voit dans « Compte » le nombre de comptes, les abonnés
actifs, les paiements reçus, et peut offrir des jours d'abonnement.

**Abonnements payés en USDT (BNB Chain).** Le client choisit une formule ;
l'application lui donne un montant exact, unique à sa commande (par exemple
29,0042 USDT), et l'adresse de paiement. Il paie depuis son portefeuille
(bouton « Payer avec mon wallet », ou envoi manuel puis collage de
l'identifiant de transaction). Le serveur lit alors la blockchain et n'active
l'abonnement que si la transaction a réussi, envoie le bon jeton à la bonne
adresse pour le montant exact, est postérieure à la commande, a reçu 5
confirmations et n'a jamais servi. Le serveur ne détient aucune clé : il ne
fait que lire. Les abonnements restent fermés tant que `pay_address` est vide.

**Paywall.** Avec `"paywall": false` (par défaut) tout le contenu est visible
par tous. Avec `true`, les visiteurs voient les cryptos et leurs scores, mais
les niveaux (entrée, stop, objectifs), les raisons et le graphique sont
réservés aux abonnés.

**Trader sur Pay212 DEX.** Pour un jeton de BNB Chain dont le contrat est connu (jetons
Binance Alpha), le bouton « Trader sur Pay212 DEX » ouvre le DEX sur ce
jeton : le client signe l'échange avec son propre portefeuille. L'adresse du
DEX se règle dans `dex_url`, où `{address}` est remplacé par le contrat du
jeton. Pour une paire Binance, le bouton ouvre la paire sur Binance.

**Trader sur Coinbase.** Dans « Compte », le titulaire relie son compte
Coinbase avec une clé API créée sur portal.cdp.coinbase.com (permissions View
et Trade, type ECDSA). Une clé qui a la permission Transfer est refusée : une
clé acceptée ne peut donc pas retirer d'argent. La clé privée est chiffrée
avant d'être écrite sur le disque, avec la clé maîtresse `APP_SECRET_KEY` du
fichier `.env` (créée par l'installation ; sans elle le trading reste coupé).

Dans l'écran Trader, « Préparer l'ordre » vérifie le prix réel sur Coinbase,
recalcule le stop et l'objectif avec les mêmes écarts que le signal, et
affiche un récapitulatif. Rien n'est acheté avant « Confirmer l'achat ».
L'achat se fait au prix du marché, puis une vente à deux branches (objectif
ou stop) est placée pour la quantité achetée. Si cette protection ne peut pas
être placée, l'application le dit clairement et la position apparaît « SANS
STOP ». Garde-fous : montant minimal et maximal par ordre, plafond par
24 heures, refus si le prix Coinbase s'écarte de plus de 3 % du prix du
signal, un seul ordre à la fois par compte. Il n'y a aucun achat automatique.

Par défaut seul l'administrateur peut trader ; `"trading_clients": true`
l'ouvre aux abonnés actifs. Garder les clés de trading de clients engage ta
responsabilité : à n'ouvrir qu'après avoir renforcé la sécurité des comptes
et vérifié le cadre légal.

Après un changement de `config.json` :
`sudo systemctl restart smart-trader-pro-web`.

## Le bilan des alertes : ce qu'elles ont vraiment donné

Chaque alerte envoyée est suivie automatiquement. Le scanner rejoue le plan
annoncé sur les bougies qui ont suivi, avec les règles du backtest : entrée à
l'ouverture de la bougie suivante, sortie à l'objectif 2, au stop ou après 72
bougies, stop prioritaire si une bougie touche les deux, frais de 0,1 % par
ordre déduits. La page affiche, par type d'alerte, le nombre d'alertes
terminées, la part de gagnantes et le résultat moyen en R (multiple du risque
pris). La liste « Derniers signaux » indique le résultat de chaque alerte.

Les cassures retenues par le filtre de marché sont suivies elles aussi, sur
une ligne à part : c'est ce qui permet de voir si le filtre évite des pertes
ou fait rater des gains. Il faut au moins une trentaine d'alertes terminées
par ligne avant d'en tirer une conclusion. Rien n'est acheté : c'est un suivi
sur le papier.

## Vérifier avant de faire confiance : le backtest

```bash
cd /opt/smart-trader-pro
sudo -u smarttrader .venv/bin/python -m smart_trader_pro backtest --symbols BTCUSDT ETHUSDT SOLUSDT --candles 5000
```

Hypothèses prudentes : entrée à l'ouverture de la bougie suivante, stop
prioritaire si stop et objectif sont touchés dans la même bougie, frais de
0,1 % par ordre. Regarde surtout `R moyen` (au-dessus de 0 = stratégie
gagnante sur la période) et compare à la colonne `Garder`.

## Comparer des variantes sans se mentir

```bash
cd /opt/smart-trader-pro
sudo -u smarttrader .venv/bin/python -m smart_trader_pro compare
```

La commande coupe l'historique en deux. Elle mesure 24 variantes (tendance
lue en 4 h ou à la journée, score minimal, largeur du stop, objectif) sur la
première partie, puis juge la meilleure sur la seconde, qu'elle n'a jamais
vue. Seul ce second résultat compte. Le verdict dit si la variante tient
(au moins 30 trades, R moyen ≥ +0,10, PF ≥ 1,15) ou non.

Régler les paramètres jusqu'à obtenir un beau backtest ne prouve rien ; une
stratégie qui tient sur des données jamais vues est un indice sérieux, pas
une garantie.

## Réglages (`config.json`)

| Clé | Défaut | Rôle |
| --- | --- | --- |
| `symbols` | `"auto"` | `"auto"` = les plus liquides, ou une liste `["BTCUSDT", "ETHUSDT"]` |
| `auto_count` | 20 | nombre de paires en mode auto |
| `interval` | `"1h"` | unité de temps des signaux (la tendance est lue 4 fois plus haut) |
| `capital`, `risk_pct` | 1000, 1 | servent au calcul de la taille de position |
| `btc_filter` | `true` | suspend les signaux si le BTC est baissier |
| `market_drop_block_pct` | 5 | suspend les signaux si le marché chute de ce pourcentage en 24 h |
| `cooldown_hours` | 12 | délai minimal entre deux alertes sur une paire |
| `strategy.min_score` | 70 | plus haut = moins de signaux, plus sélectifs |
| `radar` | `true` | active le signal Binance |
| `radar_interval` | `"15m"` | bougies du signal Binance |
| `radar_count` | 1000 | nombre maximal de paires analysées (1000 = toutes) |
| `radar_min_volume` | 0 | volume minimal sur 24 h, en USDT ; 0 = toutes les paires USDT de Binance |
| `radar_cooldown_hours` | 24 | délai minimal entre deux alertes sur une même paire |
| `pressure` | `true` | active les alertes de pré-explosion |
| `pressure_max_alerts` | 3 | alertes de pré-explosion au plus, par passage et par source |
| `pressure_telegram` | `false` | `true` : envoie aussi les pré-explosions sur Telegram |
| `pressure_strategy.min_score` | 75 | seuil de la pré-explosion |
| `alpha` | `true` | active l'analyse des jetons Binance Alpha |
| `alpha_count` | 120 | nombre maximal de jetons Alpha analysés |
| `alpha_min_volume` | 200000 | volume minimal sur 24 h, en dollars |
| `alpha_min_liquidity` | 50000 | liquidité minimale, en dollars |
| `tracking` | `true` | suit le résultat réel de chaque alerte (bilan sur la page) |
| `paywall` | `false` | `true` : niveaux, raisons et graphique réservés aux abonnés |
| `plans` | 9 / 29 / 249 USDT | formules : libellé, durée en jours, prix |
| `pay_address` | vide | adresse BNB Chain qui reçoit les abonnements ; vide = abonnements fermés |
| `pay_confirmations` | 5 | confirmations exigées avant d'activer un abonnement |
| `surge` | `true` | voie rapide « DÉPART EN COURS », vérifiée chaque minute |
| `surge_min_move`, `surge_vol_mult` | 2.0, 8 | hausse minimale en % sur 3 minutes ; volume par rapport au rythme des 24 h |
| `surge_min_volume` | 500000 | volume minimal sur 24 h, en USDT |
| `surge_cooldown_hours`, `surge_max_alerts` | 6, 2 | une alerte rapide par crypto sur cette durée ; alertes au plus par vérification |
| `min_quality` | `"C"` | `"B"` : seules les alertes notées A ou B partent sur Telegram ; `"A"` : les A seulement |
| `onchain` | `true` | flux on-chain des jetons Alpha (demande la clé CoinGecko payante) |
| `onchain_per_pass` | 8 | jetons interrogés par passage, un crédit CoinGecko chacun |
| `trading` | `true` | trading sur Coinbase depuis l'application |
| `trading_clients` | `false` | `true` : ouvert aux abonnés actifs ; `false` : administrateur seul |
| `trade_min`, `trade_max_order`, `trade_max_day` | 5, 100, 300 | montant minimal et maximal par ordre, plafond par 24 h et par compte |
| `trade_max_drift_pct` | 3 | écart maximal toléré entre le prix du signal et le prix Coinbase |
| `dex_url` | vide | adresse du DEX, avec `{address}` à la place du contrat du jeton |
| `radar_strategy.min_score` | 70 | seuil du signal Binance |
| `radar_strategy.market_bars` | 672 | filtre de marché : moyenne du Bitcoin sur N bougies ; 0 = sans filtre |
| `wide` | `true` | active le balayage du marché large |
| `wide_telegram` | `true` | `false` : les réveils restent sur la page, sans message Telegram |
| `wide_pages` | 16 | pages de 250 cryptos (16 = 4000) |
| `wide_interval_min` | 15 | minutes entre deux balayages |
| `wide_min_volume` | 300000 | volume minimal sur 24 h, en dollars |
| `wide_min_score` | 70 | seuil d'alerte du marché large |
| `exclude` | actions tokenisées | paires que le signal Binance ignore |
| `levels` | `true` | calcule VWAP, profil de volume et flux d'ordres |
| `news` | `true` | charge les actualités |
| `exchanges` | `true` | vérifie le volume sur Coinbase, OKX et Gate.io |

## Utilisation manuelle

```bash
python -m smart_trader_pro scan        # une passe
python -m smart_trader_pro watch       # surveillance continue
python -m smart_trader_pro backtest    # test sur l'historique
python -m smart_trader_pro compare     # variantes, validées sur données jamais vues
python -m smart_trader_pro web         # tableau de bord web
python -m pytest                  # tests du code
```

## Limites à connaître

- Aucun indicateur ne devine l'avenir. Le scanner repère des configurations
  favorables ; une partie finira au stop, c'est prévu dans le calcul.
- Un bon backtest ne garantit pas la suite, surtout si l'on règle les
  paramètres jusqu'à obtenir un beau résultat.
- Ne risque que de l'argent que tu peux perdre. Ceci n'est pas un conseil
  financier.

© 2026 Aziz Mernissi. Tous droits réservés.
