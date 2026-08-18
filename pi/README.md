# Abbie: kamerabaseret træning

Kører på en Raspberry Pi 5 (`abbie`) hjemme. To Xiaomi-kameraer overvåger
zoner i Abbies rum. Går hun til babygitteret, siger iPad'en "nej". Lægger hun
sig derefter i en kurv, siger den "dygtig".

Web: <https://abbie.deveo.dk> (én kode, cookie-login)
Adgang: `ssh nicolai@abbie` over Tailscale, virker fra ethvert netværk

## Delene

| Fil | Rolle |
|---|---|
| `abbie.py` | Detektor og webserver. Én ffmpeg pr. kamera, zone-diff, Pushover, log, billeder |
| `index.html` | Web-interface: live streams, zone-editor, log, pause, manuelle knapper |
| `config.example.json` | Zoner, tærskler, lyde. Den rigtige med nøgler ligger kun på Pi'en |
| `go2rtc.example.yaml` | Kamera-streams. HD til visning, `subtype=sd` til detektion |
| `Caddyfile` | Adgangskode foran alt, ruter mellem monitor og go2rtc |
| `systemd/` | Services, alle med `Restart=always` og aktiveret ved boot |

## Regler i adfærden

Bevidste valg. Lav dem ikke om uden at spørge Nicolai.

- **"dygtig" kræver et forudgående "nej"** siden sidste dygtig. Også aller
  første gang og efter en genstart. Det matcher den træning hun allerede kender.
- **Reglen tjekkes når bevægelsen ses**, ikke når lyden skal afspilles. Ellers
  ligger gammel bevægelse i kurven og kasserer ind i samme sekund et "nej"
  armerer reglen, hvilket ligner ros for ingenting.
- **Der skal gå mindst 8 sekunder fra "nej" til "dygtig" falder**
  (`sequence_min_seconds`). Gitteret og kurv 2 sidder på samme kamera, så én
  genstand der krydser billedet kan ramme begge zoner i samme sekund. 18.
  august udløste to dygtig 4 sekunder efter det nej der armerede dem, hvor de
  elleve rigtige den dag tog 11-21 sekunder. Grænsen måles til det øjeblik
  lyden falder, altså inklusive kurvens delay på 3,5 sekunder, for målt på
  bevægelsen alene ville 8 sekunder også have ramt den korteste rigtige.
  Reglen udskyder i praksis mere end den fjerner: bliver hun liggende,
  kommer rosen få sekunder senere i stedet.
- **En blokeret udløsning må aldrig bruge zonens cooldown.** `last_fired`
  sættes først når lyden faktisk afspilles.
- **En zone skal ændre sig mindst `local_ratio` gange mere end resten af
  billedet.** Kameraets automatiske lysjustering ændrer hele billedet på én
  gang, og det talte før som bevægelse i alle zoner samtidig.
- **Manuelle knapper i UI'et ignorerer både pause og sekvensregel.** Nicolai
  trykker bevidst.
- **Ingen automatisk sikkerhedsventil.** Der var én; den lukkede systemet ned i
  en time og blev fjernet igen.
- Cooldown: kurvene 0 sekunder, gitteret 10.
- **Der gemmes kun billeder når en lyd faktisk bliver afspillet.** Blokerede
  udløsninger og de tusinder af detektioner om dagen gemmer ingenting.

## Billeder ved hver lyd

Hver gang en lyd bliver afspillet, hentes et JPEG fra det kamera der udløste
den, og stien lægges i eventet i loggen. I web-interfacet er loggen ren tekst;
de to tællere (`nej` og `dygtig`) åbner en oversigt over dagens billeder med
zonen tegnet ovenpå. Det er svaret på om hun faktisk var henne ved
gitteret, som procenterne ikke kan give.

- **Billedet er fuld opløsning**, 2304x1296 fra mi360 og 1920x1080 fra
  c500pro, ca. 120-160 kB pr. styk. Detektionen kører stadig på substreamen.
- **En ffmpeg pr. kamera dekoder HD-streamen løbende** ved 2 fps og holder de
  sidste 10 sekunder som JPEG i hukommelsen. Når en lyd går, tages frame'et fra
  netop det øjeblik ud af bufferen. Ingen ventetid.
- **Derfor er go2rtc's `frame.jpeg` kun fallback.** Den venter på næste keyframe
  i H265-streamen, og målt på Pi'en gav det billeder 2-3 sekunder efter lyden.
  Den bruges nu kun hvis bufferen er tom, fx lige efter en genstart.
- **Prisen er ca. 27 % af én kerne pr. kamera.** Pi 5 har fire, og
  detektionen bruger 2,4 %. Skru ned med `"snapshots": {"fps": 1}` eller slå
  bufferen fra med `"live": false`, så falder den tilbage til go2rtc.
- **Bemærk:** HD-streamen står nu åben hele tiden, hvor den før kun kørte når du
  havde siden åben. Det er samme antal p2p-sessioner som når du ser med, men
  nu permanent.
- **Kameraets indbrændte ur står 1-2 sekunder bag vægguret**, fordi streamen er
  forsinket. Detektoren læser en lige så forsinket stream, så billedet passer
  til det frame der udløste, også når urene ikke matcher log-tiden.
- **Billedet tages når lyden afspilles.** For `dygtig` er der 3,5 sekunders
  delay, så billedet viser det øjeblik rosen falder, ikke det øjeblik hun trådte
  i kurven.
- Ligger i `/opt/abbie/snapshots/<dato>/` og ryddes med samme `keep_days` som
  loggen. En dag med 70 lyde er ca. 10 MB, 90 dage under 1 GB.
- Slå det helt fra med `"snapshots": {"enabled": false}`.

## Fælder der har kostet tid

- Nyt endpoint i `abbie.py` skal **også** tilføjes i `@monitor path` i
  `Caddyfile`, ellers ryger det til go2rtc og giver 404.
- Basic auth kan ikke bruges. go2rtc's afspiller er ES-moduler, og
  modul-hentninger sender ikke basic auth-legitimation. Derfor cookie-login.
- Xiaomi-kameraerne tillader kun få samtidige p2p-sessioner. Efterladte
  teststreams i `go2rtc.yaml` får alt til at time out.
- Detektion kører på substreams, ikke fuld opløsning: 2,4 % CPU mod 14,6 %.

## Adgang

Tre veje ind, prøv i denne rækkefølge. `deploy.sh` gør det automatisk.

| Vej | Kommando | Virker |
|---|---|---|
| Tailscale | `ssh nicolai@abbie` | de fleste netværk |
| Tailscale-IP | `ssh nicolai@100.91.217.86` | hvis navneopslag driller |
| LAN | `ssh nicolai@192.168.86.44` | kun hjemme |

Ting der har spærret vejen før, i den rækkefølge de er værd at tjekke:

1. **Tailscale står på "stopped".** Start den: `/Applications/Tailscale.app/Contents/MacOS/Tailscale up`
2. **Et LAN-kabel sidder i.** Fortinet-udstyret på Nicolais arbejdsnet blokerer
   Tailscale, og et USB-dock på ethernet får forrang over WiFi. Træk kablet ud,
   så ryger trafikken over WiFi eller hotspot.
3. **SSH-nøglen har en adgangssætning** og ligger ikke altid i ssh-agent. Over
   Tailscale er det uden betydning, for Tailscale SSH bruger tailnet-identitet.
   Til LAN-vejen: `ssh-add --apple-use-keychain ~/.ssh/id_ed25519`
4. **Pi'en er selv nede.** `curl -s -o /dev/null -w '%{http_code}' https://abbie.deveo.dk/login`
   giver 530 hvis Cloudflare ikke kan nå den. Så hjælper ingen SSH-vej.

### Læs data uden SSH

Tunnelen virker på netværk hvor Tailscale ikke gør, og giver adgang til loggen:

```bash
J=$(mktemp)
curl -s -c "$J" -o /dev/null -X POST -d "code=DIN_KODE" https://abbie.deveo.dk/login
curl -s -b "$J" "https://abbie.deveo.dk/history?day=$(date +%F)" | python3 -m json.tool
curl -s -b "$J" https://abbie.deveo.dk/state
```

Endpoints: `/state`, `/history?day=`, `/days`, `/config`, `/events` (SSE),
`/snap?f=<dato>/<fil>.jpg`.

`/history` returnerer de sidste 3000 linjer af dagen, og på en travl dag er de
alle detektioner. Vil du kun have de afspillede lyde, og dem alle, så brug
`&only=sounds`:

```bash
curl -s -b "$J" "https://abbie.deveo.dk/history?day=$(date +%F)&only=sounds" \
  | python3 -m json.tool
```

## Deploy

```bash
./pi/deploy.sh                 # abbie.py og index.html
./pi/deploy.sh index.html      # kun én fil
```

Scriptet finder selv en åben vej ind, lægger filerne på plads, genstarter
tjenesten og verificerer med checksum at det der ligger på Pi'en er det du
sendte. Fejler noget, printer den de sidste linjer fra tjenestens log.

`deploy.sh` sender kun `abbie.py` og `index.html`. Rører du `Caddyfile`, skal
den lægges på plads i hånden, ellers rammer nye endpoints go2rtc og giver 404:

```bash
scp pi/Caddyfile nicolai@abbie:~/ && ssh nicolai@abbie \
  'sudo install -m 644 ~/Caddyfile /etc/caddy/Caddyfile && sudo systemctl reload caddy'
```

Push **aldrig** `config.json` fra en lokal kopi: zoner redigeres i browseren og
skrives direkte på Pi'en, så en blind overskrivning smider dem væk. Skal en
indstilling ændres, så ret den på Pi'en:

```bash
ssh nicolai@abbie 'sudo nano /opt/abbie/config.json && sudo systemctl restart abbie'
```

## Når noget går galt

Systemloggen ligger på disk (maks 60 MB), så den overlever en genstart:

```bash
ssh nicolai@abbie 'journalctl -u abbie -n 50 --no-pager'
ssh nicolai@abbie 'journalctl --list-boots'          # nedbrud ses som manglende afslutning
ssh nicolai@abbie 'journalctl -b -1 -p err --no-pager'   # fejl i forrige opstart
ssh nicolai@abbie 'vcgencmd get_throttled'           # 0x0 = ingen strømproblemer
```

18. august 2026 svarede Pi'en på ARP, men hverken ping eller nogen port. Efter
en strømafbrydelse kom den op igen, filsystemet var rent, og årsagen blev aldrig
fundet: systemloggen lå dengang i RAM og forsvandt. Derfor ligger den nu på disk.
