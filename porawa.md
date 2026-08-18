Napisz prostą i lekką aplikację Python działającą na Linuxie.

Cel:

```text
MSSQL
↓
proste SELECT
↓
wykrycie nowego rekordu
↓
wysłanie wiadomości na kanał Zello
```

Aplikacja ma obsługiwać:

1. wiadomość tekstową Zello,
2. opcjonalnie wcześniej nagraną wiadomość głosową WAV.

Nie twórz rozbudowanej architektury enterprise.

Nie dodawaj:

* Redis,
* Celery,
* RabbitMQ,
* FastAPI,
* healthcheck HTTP,
* wielu workerów,
* Kubernetes,
* rozbudowanych kolejek,
* ORM.

Użyj tylko tego, co naprawdę potrzebne.

## Technologie

Linux.

Użyj aktualnych stabilnych wersji:

```text
Python 3.14
pyodbc
websockets
python-dotenv
FFmpeg
libopus
Microsoft ODBC Driver 18 for SQL Server
```

## Struktura

Projekt ma być mały:

```text
zello-bot/
├── main.py
├── zello.py
├── audio.py
├── .env
├── .env.example
├── requirements.txt
└── README.md
```

Jeżeli logika nadal będzie czytelna, można nawet połączyć `zello.py` i `audio.py`.

Nie twórz niepotrzebnych warstw abstrakcji.

## Konfiguracja

`.env`:

```env
MSSQL_SERVER=192.168.1.10
MSSQL_DATABASE=ERP
MSSQL_USERNAME=zello_bot
MSSQL_PASSWORD=HASLO

ZELLO_NETWORK=moja_siec
ZELLO_USERNAME=sql_bot
ZELLO_PASSWORD=HASLO
ZELLO_CHANNEL=Magazyn

POLL_INTERVAL=3

SEND_TEXT=true
SEND_VOICE=true

VOICE_FILE=audio/new_order.wav
```

Sekretów nie wpisuj w kodzie.

## MSSQL

Użyj `pyodbc`.

Connection string ma używać:

```text
ODBC Driver 18 for SQL Server
```

Zapytanie ma być bardzo łatwe do zmiany.

Przykład:

```sql
SELECT TOP 1
    id,
    order_number
FROM dbo.orders
WHERE id > ?
ORDER BY id ASC;
```

Aplikacja ma pamiętać ostatnio obsłużone ID.

Utwórz prostą tabelę:

```sql
CREATE TABLE dbo.zello_bot_state (
    bot_name NVARCHAR(100) PRIMARY KEY,
    last_id BIGINT NOT NULL
);
```

Przykładowy rekord:

```sql
INSERT INTO dbo.zello_bot_state
    (bot_name, last_id)
VALUES
    ('orders', 0);
```

Program ma pobierać:

```sql
SELECT last_id
FROM dbo.zello_bot_state
WHERE bot_name = 'orders';
```

Następnie:

```sql
SELECT TOP 1
    id,
    order_number
FROM dbo.orders
WHERE id > ?
ORDER BY id ASC;
```

Po poprawnym wysłaniu powiadomienia:

```sql
UPDATE dbo.zello_bot_state
SET last_id = ?
WHERE bot_name = 'orders';
```

Dzięki temu po restarcie programu nie wysyłaj ponownie starych rekordów.

## Pierwsze uruchomienie

Nie wysyłaj wszystkich historycznych zamówień.

Jeżeli wpis `orders` nie istnieje w `zello_bot_state`, wykonaj:

```sql
SELECT ISNULL(MAX(id), 0)
FROM dbo.orders;
```

i zapisz aktualne największe ID jako punkt startowy.

Dopiero nowe rekordy mają powodować powiadomienie.

## Polling

Pętla ma wyglądać logicznie tak:

```python
while True:

    last_id = get_last_id()

    order = get_next_order(last_id)

    if order:
        send_notification(order)

        update_last_id(order.id)

    else:
        await asyncio.sleep(POLL_INTERVAL)
```

Nie komplikuj tego.

## Wiadomość tekstowa

Przykład:

```text
🔔 Nowe zamówienie: ZAM/2026/1234
```

Użyj oficjalnego Zello Channel API.

Połącz się:

```text
wss://zellowork.io/ws/{ZELLO_NETWORK}
```

Zaloguj się komendą `logon`.

Następnie użyj:

```json
{
  "command": "send_text_message",
  "seq": 10,
  "channel": "Magazyn",
  "text": "Nowe zamówienie"
}
```

Poczekaj na odpowiedź:

```json
{
  "seq": 10,
  "success": true
}
```

Dopiero wtedy uznaj tekst za wysłany.

## Połączenie Zello

Nie łącz się ponownie dla każdego zamówienia.

Połączenie WebSocket powinno pozostawać otwarte.

Jeżeli zostanie zerwane:

```text
sleep 5 sekund
połącz ponownie
zaloguj się ponownie
kontynuuj
```

Nie implementuj skomplikowanego exponential backoff.

Wystarczy stałe:

```text
5 sekund
```

## Głos

Jeżeli:

```env
SEND_VOICE=true
```

po wykryciu nowego zamówienia wyślij plik:

```text
VOICE_FILE
```

np.:

```text
audio/new_order.wav
```

Nagranie jest zawsze takie samo, np.:

```text
"Uwaga. Pojawiło się nowe zamówienie."
```

Nie generuj TTS.

Nie podstawiaj danych zamówienia do audio.

To jest wcześniej nagrany komunikat.

## Audio

Plik WAV może być w normalnym formacie.

FFmpeg ma przekonwertować go w locie do:

```text
16000 Hz
mono
signed 16-bit PCM
```

Przykładowo:

```bash
ffmpeg \
  -hide_banner \
  -loglevel error \
  -i audio/new_order.wav \
  -f s16le \
  -ac 1 \
  -ar 16000 \
  pipe:1
```

PCM podziel na ramki:

```text
20 ms
```

Dla:

```text
16000 Hz
mono
16-bit
```

jedna ramka ma:

```text
640 bytes
```

Każdą ramkę zakoduj do Opus przez systemową bibliotekę `libopus`.

Nie zapisuj pośredniego `.opus` na dysku.

Pipeline:

```text
WAV
↓
FFmpeg
↓
PCM 16 kHz mono
↓
libopus
↓
raw Opus packet
↓
Zello WebSocket
```

## Zello voice

Najpierw:

```json
{
  "command": "start_stream",
  "seq": 20,
  "channel": "Magazyn",
  "type": "audio",
  "codec": "opus",
  "codec_header": "...",
  "packet_duration": 20
}
```

Poczekaj na odpowiedź i pobierz:

```text
stream_id
```

Następnie każdy pakiet wysyłaj jako binary WebSocket frame:

```python
struct.pack("!BII", 0x01, stream_id, 0) + opus_packet
```

Gdzie:

```text
0x01 = audio packet
stream_id = wartość otrzymana od Zello
packet_id = 0
```

Po zakończeniu:

```json
{
  "command": "stop_stream",
  "seq": 21,
  "channel": "Magazyn",
  "stream_id": 12345
}
```

## codec_header

Dla:

```text
16000 Hz
1 frame per packet
20 ms
```

wygeneruj:

```python
codec_header_bytes = struct.pack(
    "<HBB",
    16000,
    1,
    20
)
```

następnie:

```python
codec_header = base64.b64encode(
    codec_header_bytes
).decode("ascii")
```

## Tempo audio

Nagranie musi być wysyłane mniej więcej w czasie rzeczywistym.

Jedna ramka:

```text
20 ms
```

czyli około:

```text
50 pakietów / sekundę
```

Nie wysyłaj całego nagrania natychmiast.

Najprostsze rozwiązanie:

```python
await asyncio.sleep(0.02)
```

pomiędzy ramkami jest wystarczające dla tego prostego zastosowania.

Nie implementuj skomplikowanego scheduler'a audio, jeśli nie jest potrzebny.

## libopus

Jeżeli nie ma prostej aktualnej biblioteki Python kompatybilnej z Python 3.14, użyj `ctypes`.

Potrzebne są tylko:

```text
opus_encoder_create
opus_encode
opus_encoder_destroy
```

Encoder:

```text
sample rate = 16000
channels = 1
application = OPUS_APPLICATION_VOIP
```

Nie buduj osobnego dużego wrappera.

## Kolejność

Dla nowego zamówienia:

```text
SELECT
↓
nowe ID
↓
wyślij tekst
↓
jeżeli SEND_VOICE=true:
    wyślij WAV jako voice
↓
jeżeli wszystko się uda:
    zapisz last_id
```

Jeżeli wysyłanie się nie uda:

```text
NIE zmieniaj last_id
```

Po kilku sekundach program może spróbować ponownie.

## Retry

Nie twórz tabeli retry.

Nie twórz kolejki.

Jeżeli wystąpi błąd:

```python
logging.exception(...)
await asyncio.sleep(5)
```

i spróbuj ponownie.

To wystarczy.

## Ważne

Jeżeli:

```text
tekst wysłany poprawnie
```

ale:

```text
voice nie został wysłany
```

i `last_id` nie zostanie zapisane, przy kolejnej próbie tekst może zostać wysłany ponownie.

Dla tej prostej wersji jest to akceptowalne.

Jeżeli chcemy później wyeliminować także ten przypadek, można rozbudować tabelę stanu o osobne `text_sent` i `voice_sent`.

Na tym etapie nie komplikuj rozwiązania.

## Logowanie

Proste logi:

```text
INFO Connected to MSSQL
INFO Connected to Zello
INFO New order id=1234
INFO Text sent
INFO Sending voice
INFO Voice sent
INFO Updated last_id=1234
```

Przy błędzie:

```text
ERROR Zello disconnected
ERROR Voice transmission failed
```

Nie zapisuj haseł.

## Testowanie

Dodaj tylko trzy proste polecenia:

```bash
python main.py --test-db
```

sprawdza:

```sql
SELECT 1
```

oraz:

```bash
python main.py --test-text
```

wysyła:

```text
Test wiadomości z bota MSSQL
```

oraz:

```bash
python main.py --test-voice
```

wysyła:

```text
VOICE_FILE
```

na kanał Zello.

## Uruchomienie

Normalnie:

```bash
python main.py
```

## systemd

Przygotuj prosty:

```text
zello-bot.service
```

uruchamiający:

```text
/opt/zello-bot/.venv/bin/python /opt/zello-bot/main.py
```

oraz:

```ini
Restart=always
RestartSec=5
```

Nie dodawaj Dockera, jeżeli nie jest konieczny.

Preferuję zwykły:

```text
Python virtualenv + systemd
```

ponieważ rozwiązanie ma być lekkie.

## README

Podaj instrukcję instalacji na aktualnym Debian/Ubuntu Linux.

Uwzględnij:

```text
Python
virtualenv
Microsoft ODBC Driver 18
unixODBC
FFmpeg
libopus
```

Następnie:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

i:

```bash
python main.py --test-db
python main.py --test-text
python main.py --test-voice
python main.py
```

## Najważniejsze wymaganie

Kod ma być prosty.

Preferuję:

```text
300-500 linii dobrego kodu
```

zamiast:

```text
kilku tysięcy linii
```

Nie projektuj systemu na milion wiadomości.

To ma obsługiwać prosty przypadek:

```text
co kilka sekund SELECT z MSSQL
↓
pojawił się nowy rekord
↓
powiadom Zello
```

Na końcu wygeneruj kompletne pliki, a nie pseudokod.

Uruchom testy lokalne i pokaż wynik.
