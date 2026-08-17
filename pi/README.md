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

## Deploy

```bash
scp pi/abbie.py pi/index.html nicolai@abbie:~/abbie-deploy/
ssh nicolai@abbie '
  sudo install -o nicolai -g nicolai -m 0755 ~/abbie-deploy/abbie.py  /opt/abbie/abbie.py
  sudo install -o nicolai -g nicolai -m 0644 ~/abbie-deploy/index.html /opt/abbie/index.html
  sudo systemctl restart abbie'
```

Push **aldrig** `config.json` fra en lokal kopi: zoner redigeres i browseren og
skrives direkte på Pi'en, så en blind overskrivning smider dem væk.
