# Zello Bot — MSSQL → Zello (tekst + głos)

Lekki notifier: co `POLL_INTERVAL` sekund wykonuje zapytanie z pliku
**`query.sql`**; jeśli zwróci wiersz — wysyła powiadomienie (tekst i/lub
wcześniej nagrany głos WAV) na kanał Zello. **Bot nie pamięta obsłużonych
zamówień** — to samo zamówienie może dostać powiadomienie przy każdym pollingu,
dopóki zapytanie je zwraca.

```text
MSSQL (read-only) → SELECT z query.sql → wiersz? → Zello (tekst, opcjonalnie głos WAV) → powtórz o interwał
```

## Struktura

```text
.
├── main.py          # punkt wejścia: serwis + komendy testowe
├── config.py        # konfiguracja z .env
├── db.py            # połączenie MSSQL (pyodbc) + ładowanie query.sql
├── query.sql        # ← ZAPYTANIE — edytujesz TEN plik, nie kod
├── zello.py         # klient Zello Channel API (Work / Friends & Family)
├── audio.py         # WAV → FFmpeg → PCM 16 kHz → libopus → ramki 20 ms
├── tests/           # testy: config, baza, pętla serwisu, zello, audio
├── audio/           # tu wrzuć plik głosowy (VOICE_FILE)
├── .env.example     # wzorzec konfiguracji
├── zello-bot.service
└── README.md
```

## Jak działa

1. Baza jest traktowana jako **tylko do odczytu** — bot wykonuje wyłącznie
   `SELECT` (connection string zawiera `ApplicationIntent=ReadOnly`; konto
   MSSQL potrzebuje tylko `GRANT SELECT`).
2. Zapytanie czyta z **`query.sql`** (fail-fast przy starcie: brak pliku,
   pusty plik lub nie-SELECT = jasny błąd w logu).
3. Pętla: `SELECT TOP 1 ...` → jeśli wiersz → wyślij powiadomienie (tekst i/lub
   głos wg `SEND_TEXT` / `SEND_VOICE`) → odczekaj `POLL_INTERVAL` → powtórz.
4. Połączenie WebSocket z Zello trzymane otwarte; po zerwaniu: 5 s przerwy,
   ponowne połączenie i logowanie (bez exponential backoff).
5. Wiadomość uznajemy za wysłaną **dopiero po** odpowiedzi
   `{"seq": N, "success": true}`.

Kolejność dla jednego powiadomienia: tekst → głos. Jeśli głos się nie powiedzie,
przy następnym pollingu powiadomienie zostanie wysłane ponownie — dla tej
prostej wersji świadomie zaakceptowane.

## Instalacja (Debian / Ubuntu)

**1. Pakiety systemowe:**

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip ffmpeg libopus0 curl gnupg ca-certificates
```

**2. Microsoft ODBC Driver 18 for SQL Server:**

```bash
curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | sudo gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg
curl -fsSL https://packages.microsoft.com/config/ubuntu/24.04/prod.list | sudo tee /etc/apt/sources.list.d/mssql-release.list
sudo apt-get update
sudo ACCEPT_EULA=Y apt-get install -y msodbcsql18 unixodbc
```

> Ubuntu 22.04: w adresie repo zamień `24.04` na `22.04`. Debian: patrz
> [dokumentacja Microsoft](https://learn.microsoft.com/pl-pl/sql/connect/odbc/linux-mac/installing-the-microsoft-odbc-driver-for-sql-server).

**3. Projekt + środowisko:**

```bash
cd /opt/zello-bot
sudo chown -R $USER:$USER /opt/zello-bot    # tylko gdy katalog zakładałeś przez sudo
python3 -m venv .venv                       # NIE przez sudo!
source .venv/bin/activate
pip install -r requirements.txt
```

**4. Konfiguracja:**

```bash
cp .env.example .env
nano .env          # uzupełnij MSSQL + Zello (wzór niżej)
nano query.sql     # wpisz nazwę swojej tabeli zamówień
```

**5. Test i start:**

```bash
python main.py --test-db        # SELECT 1 + walidacja query.sql
python main.py --test-text      # test Zello (tekst)
python main.py --test-voice     # test Zello (głos)
python main.py                  # serwis 24/7
```

### Wzór `.env`

```env
# --- MSSQL (jak w działającej aplikacji) ---
MSSQL_SERVER=192.168.24.22  # instancja nazwana, BEZ portu
MSSQL_DATABASE=1
MSSQL_USERNAME=2
MSSQL_PASSWORD=haslo
MSSQL_ENCRYPT=yes                          # stary serwer bez TLS 1.2 → no
MSSQL_TRUST_SERVER_CERTIFICATE=yes

# --- Zello Work (wss://zellowork.io/ws/{siec}) ---
ZELLO_NETWORK=moja_siec
ZELLO_USERNAME=sql_bot
ZELLO_PASSWORD=haslo
ZELLO_CHANNEL=Magazyn

# --- LUB Zello Friends & Family (darmowe, wss://zello.io/ws) ---
# Token z https://developers.zello.com/ (Keys → Sample Development Token,
# ważny 30 dni). Gdy ustawiony, ZELLO_NETWORK jest ignorowany.
ZELLO_AUTH_TOKEN=

# --- Zachowanie ---
POLL_INTERVAL=3
SEND_TEXT=true
SEND_VOICE=true
VOICE_FILE=audio/new_order.wav
```

### Wzór `query.sql`

Jedyne miejsce, które dostosowujesz do swojej bazy (bez ruszania kodu):

```sql
SELECT TOP 1
    id,
    order_number
FROM dbo.orders          -- ← nazwa Twojej tabeli
WHERE id > 0             -- ← Twój warunek, np. status = 'oczekuje'
ORDER BY id ASC;
```

Wymagania: max 1 wiersz; kolumny `id` (do logów) i `order_number` (numer
w wiadomości). Komentarze `--` na początku pliku są pomijane.

## Testy przed startem

```bash
python main.py --test-db        # SELECT 1 + walidacja query.sql
python main.py --test-text      # "Test wiadomości z bota MSSQL" na kanał Zello
python main.py --test-voice     # VOICE_FILE na kanał Zello
```

Kod 0 = sukces, kod != 0 = porażka.

## Uruchomienie

```bash
python main.py
```

Oczekiwane logi:

```text
INFO Connected to MSSQL
INFO Connected to Zello
INFO Channel Magazyn online
INFO New order id=1234
INFO Text sent
INFO Sending voice
INFO Voice sent
```

## systemd (serwis 24/7)

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin zello-bot
sudo chown -R zello-bot:zello-bot /opt/zello-bot
sudo chmod 600 /opt/zello-bot/.env

sudo cp zello-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now zello-bot
```

```bash
systemctl status zello-bot
journalctl -u zello-bot -f
```

## Głos (SEND_VOICE)

`VOICE_FILE` to zwykły WAV (np. nagranie: „Uwaga. Pojawiło się nowe
zamówienie."). Bot nie generuje TTS. Pipeline: `WAV → ffmpeg (-f s16le -ac 1
-ar 16000) → PCM → libopus (ctypes) → ramki 20 ms → Zello (start_stream →
dane → stop_stream)`. Plik jest kodowany raz, przy starcie (brak pośredniego
`.opus` na dysku); brak pliku = fail-fast z jasnym błędem.

## Testy jednostkowe

```bash
pip install -r requirements-dev.txt
pytest tests -q
```

WebSocket Zello, FFmpeg, libopus i MSSQL są zamockowane — nie trzeba
uruchamiać żadnych serwisów.
