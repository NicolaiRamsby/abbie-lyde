# Abbie: kamerabaseret træning

Kører på en Raspberry Pi 5 (`abbie`) hjemme. To Xiaomi-kameraer overvåger
zoner i Abbies rum. Går hun til babygitteret, siger iPad'en "nej". Lægger hun
sig derefter i en kurv, siger den "dygtig".

Web: <https://abbie.deveo.dk> (én kode, cookie-login)
Adgang: `ssh nicolai@abbie` over Tailscale, virker fra ethvert netværk

## Delene

| Fil | Rolle |
|---|---|
| `abbie.py` | Detektor og webserver. Én ffmpeg pr. kamera, zone-diff, Pushover, log |
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

Endpoints: `/state`, `/history?day=`, `/days`, `/config`, `/events` (SSE).

## Deploy

```bash
./pi/deploy.sh                 # abbie.py og index.html
./pi/deploy.sh index.html      # kun én fil
```

Scriptet finder selv en åben vej ind, lægger filerne på plads, genstarter
tjenesten og verificerer med checksum at det der ligger på Pi'en er det du
sendte. Fejler noget, printer den de sidste linjer fra tjenestens log.

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
