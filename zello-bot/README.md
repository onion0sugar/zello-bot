# Zello Bot — MSSQL → Zello (tekst + głos)

Lekki notifier: co `POLL_INTERVAL` sekund pyta MSSQL o nowy rekord, a gdy się
pojawi — wysyła wiadomość tekstową (i opcjonalnie wcześniej nagrany komunikat
głosowy WAV) na kanał **Zello Work**.

```text
MSSQL → proste SELECT → nowy rekord → Zello (tekst, opcjonalnie głos WAV)
```

Brak zewnętrznych zależności poza: `pyodbc`, `websockets`, `python-dotenv`,
`ffmpeg`, `libopus`, ODBC Driver 18.

## Jak działa

1. Baza jest traktowana jako **tylko do odczytu** — bot wykonuje wyłącznie
   `SELECT`, nigdy nic nie zapisuje (connection string zawiera
   `ApplicationIntent=ReadOnly`). Konto MSSQL potrzebuje tylko `GRANT SELECT`.
2. Bot **nie zapamiętuje** zamówień — nie ma tabeli stanu, checkpointu ani
   zapisu `last_id`.
3. Pętla: `SELECT TOP 1 ...` → jeśli zapytanie zwróciło wiersz → wyślij
   powiadomienie (tekst i/lub głos, wg `SEND_TEXT` / `SEND_VOICE`) → odczekaj
   `POLL_INTERVAL` → powtórz.
4. Jeśli zapytanie **ciągle zwraca ten sam wiersz**, powiadomienie będzie
   wysyłane za każdym razem — to zamierzone zachowanie. Warunek wyboru wiersza
   (`WHERE ...`) ustawiasz sam w `GET_NEXT_ORDER_SQL`.
5. Połączenie WebSocket z Zello jest trzymane otwarte; po zerwaniu: 5 s
   przerwy, ponowne połączenie i logowanie (bez exponential backoff).
6. Wiadomość uznajemy za wysłaną **dopiero po** odpowiedzi `{"seq": N, "success": true}`.

Kolejność dla jednego powiadomienia: tekst → głos (wg flag). Jeśli głos się nie
powiedzie, przy następnym pollingu powiadomienie zostanie wysłane ponownie —
dla tej prostej wersji jest to świadomie zaakceptowane.

## Wymagania (Debian / Ubuntu)

```bash
# Python 3.12+
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip

# Microsoft ODBC Driver 18 for SQL Server + unixODBC
curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | sudo gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg
curl -fsSL https://packages.microsoft.com/config/ubuntu/24.04/prod.list | sudo tee /etc/apt/sources.list.d/mssql-release.list
sudo apt-get update
sudo ACCEPT_EULA=Y apt-get install -y msodbcsql18 unixodbc

# Głos: FFmpeg + libopus
sudo apt-get install -y ffmpeg libopus0
```

Dla Ubuntu 22.04 podmień `24.04` na `22.04` w adresie repo; dla Debian zob.
[dokumentację Microsoft](https://learn.microsoft.com/pl-pl/sql/connect/odbc/linux-mac/installing-the-microsoft-odbc-driver-for-sql-server).

## Instalacja

```bash
cd /opt/zello-bot                      # lub gdziekolwiek chcesz

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
nano .env                              # uzupełnij dane
```

Bot nie używa żadnej tabeli stanu — nie musisz niczego tworzyć w bazie.

Użytkownik MSSQL potrzebuje tylko: `SELECT` na tabeli zamówień.
Nie używaj konta `sa`.

## Testy przed startem

```bash
python main.py --test-db       # SELECT 1 — czy baza odpowiada
python main.py --test-text     # "Test wiadomości z bota MSSQL" na kanał Zello
python main.py --test-voice    # VOICE_FILE na kanał Zello
```

Wszystkie kończą się kodem 0 przy sukcesie, kodem != 0 przy porażce.

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
chmod 600 /opt/zello-bot/.env        # sekrety tylko dla właściciela

sudo cp zello-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now zello-bot
```

Podgląd logów:

```bash
systemctl status zello-bot
journalctl -u zello-bot -f
```

## Dostosowanie zapytania o zamówienia

Wszystko w jednym miejscu — w `main.py`:

```python
GET_NEXT_ORDER_SQL = """
SELECT TOP 1
    id,
    order_number
FROM dbo.orders
WHERE id > 0          -- ← TU wpisz swój warunek (np. status = 'oczekuje')
ORDER BY id ASC;
"""
```

Podmień nazwę tabeli, kolumny i warunek `WHERE`. Wiersz musi zwracać `id`
i `order_number` (tekst wyświetlany w wiadomości). Bot nie pamięta, co już
wysłał — to warunek w `WHERE` decyduje, co jest "do wysłania" (np. status,
flaga, data). Formatu wiadomości dotyczy `DEFAULT_TEXT`:

```python
DEFAULT_TEXT = "🔔 Nowe zamówienie: {}"
```

## Głos (SEND_VOICE)

`VOICE_FILE` to zwykły WAV (np. nagranie: *„Uwaga. Pojawiło się nowe
zamówienie."*). Bot nie generuje TTS i nie podmienia danych w nagraniu —
komunikat jest zawsze ten sam. Pipeline: `WAV → ffmpeg (-f s16le -ac 1 -ar
16000) → PCM → libopus (ctypes) → ramki 20 ms → Zello (start_stream → dane →
stop_stream)`. Żadnych pośrednich plików `.opus` na dysku; nagranie jest
kodowane raz, przy starcie.

## Testy jednostkowe

```bash
pip install -r requirements-dev.txt
pytest tests -q
```

WebSocket Zello, FFmpeg, libopus i MSSQL są w testach zamockowane — nie trzeba
uruchamiać serwisów.
