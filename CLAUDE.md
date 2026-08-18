# Abbie

To projekter i samme repo, adskilt på branches.

- `main` — `index.html` i roden: den gamle manuelle Pushover-side, hvor Nicolai
  selv trykker for at afspille lyde på iPad'en.
- `pi-overvaagning` — mappen `pi/`: den automatiske kamera-baserede træning på
  en Raspberry Pi. **Det er den der er i drift.**

Læs `pi/README.md` før du ændrer noget. Den beskriver adfærdsreglerne, og de er
bevidste valg der er kostet dyrt at nå frem til.

## Sådan udruller du en ændring

```bash
./pi/deploy.sh                 # abbie.py og index.html
./pi/deploy.sh index.html      # kun én fil
```

Scriptet finder selv en åben vej ind (Tailscale, Tailscale-IP, LAN),
genstarter tjenesten og verificerer med checksum. Kan det ikke nå Pi'en,
printer det hvad du skal tjekke.

**Ret aldrig filerne direkte på Pi'en.** Ret dem i `pi/` og kør `deploy.sh`,
ellers afviger repoet fra det der kører. Undtagelsen er `config.json`, se nedenfor.

## Det der aldrig må pushes fra en lokal kopi

`config.json` på Pi'en indeholder zoner som Nicolai redigerer i browseren.
Skubber du din egen kopi over, forsvinder hans redigeringer. Skal en indstilling
ændres, så ret den på Pi'en:

```bash
ssh nicolai@abbie 'sudo nano /opt/abbie/config.json && sudo systemctl restart abbie'
```

Samme gælder `go2rtc.yaml` (indeholder Xiaomi-token) og `auth.json` (kode-hash).
De står i `.gitignore` og findes kun på Pi'en. Repoet har `.example`-udgaver.

## Før du ændrer adfærden

Reglerne i `pi/README.md` under "Regler i adfærden" er aftalt med Nicolai og
handler om en hund der lærer noget. Lav dem ikke om fordi de ser mærkelige ud i
koden. Spørg først.

Særligt: der er bevidst **ingen** automatisk sikkerhedsventil. Der var én, den
lukkede systemet ned i en time uden varsel, og den blev fjernet igen.

## Tjek at det virker bagefter

```bash
ssh nicolai@abbie 'systemctl is-active go2rtc abbie caddy cloudflared'
curl -s -o /dev/null -w '%{http_code}\n' https://abbie.deveo.dk/login   # 200
```

Frontend-ændringer skal ses i en browser, ikke kun antages. Siden kræver kode,
så log ind i browseren, eller hent data med curl som beskrevet i `pi/README.md`.
