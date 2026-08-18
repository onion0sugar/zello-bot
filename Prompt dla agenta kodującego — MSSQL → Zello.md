Zbuduj kompletną aplikację produkcyjną w Pythonie 3.12 działającą na Linuxie, której zadaniem będzie wykrywanie nowych rekordów w bazie Microsoft SQL Server i wysyłanie powiadomień tekstowych na kanał Zello.

## Cel

Aplikacja ma działać jako ciągły serwis:

MSSQL → wykrycie nowego zamówienia → utworzenie wiadomości → wysłanie wiadomości tekstowej na kanał Zello → zapisanie informacji, że rekord został obsłużony.

Przykładowa wiadomość:

```text
🔔 NOWE ZAMÓWIENIE

Numer: 15231
Klient: ABC Sp. z o.o.
Produkt: Produkt XYZ
Ilość: 4
```

## Środowisko

System:

- Linux
- Python 3.12
- Microsoft SQL Server
- Zello Work
- aplikacja uruchamiana jako serwis 24/7

Do MSSQL użyj:

```text
pyodbc
Microsoft ODBC Driver 18 for SQL Server
```

Do Zello użyj WebSocket API:

```text
wss://zellowork.io/ws/{ZELLO_NETWORK}
```

Dokumentacja:

```text
https://github.com/zelloptt/zello-channel-api
https://github.com/zelloptt/zello-channel-api/blob/main/API.md
```

Nie korzystaj z nieoficjalnego sterowania GUI Zello.

## Zello

Po połączeniu WebSocket aplikacja ma wykonać:

```json
{
  "command": "logon",
  "seq": 1,
  "username": "...",
  "password": "...",
  "channels": ["..."]
}
```

Po poprawnym zalogowaniu i otrzymaniu informacji, że kanał jest online, wiadomość tekstową należy wysłać przez:

```json
{
  "command": "send_text_message",
  "seq": 2,
  "channel": "NAZWA_KANALU",
  "text": "Treść wiadomości"
}
```

Sprawdzaj odpowiedź Zello:

```json
{
  "seq": 2,
  "success": true
}
```

Rekord z MSSQL może zostać oznaczony jako wysłany dopiero po otrzymaniu:

```text
success = true
```

Nie uznawaj samego wykonania `websocket.send()` za potwierdzenie dostarczenia.

## MSSQL

Połączenie ma być konfigurowane przez `.env`.

Przykład:

```env
MSSQL_SERVER=127.0.0.1
MSSQL_PORT=1433
MSSQL_DATABASE=moja_baza
MSSQL_USERNAME=zello_reader
MSSQL_PASSWORD=haslo
MSSQL_DRIVER=ODBC Driver 18 for SQL Server

ZELLO_NETWORK=moja_siec
ZELLO_USERNAME=sql_bot
ZELLO_PASSWORD=haslo
ZELLO_CHANNEL=Magazyn

POLL_INTERVAL_SECONDS=2
LOG_LEVEL=INFO
```

Żadnych loginów ani haseł wpisanych na stałe w kodzie.

Dodaj `.env.example`, ale nigdy nie umieszczaj prawdziwych danych dostępowych w repozytorium.

Dodaj `.env` do `.gitignore`.

## Wykrywanie nowych rekordów

Na początku przygotuj system tak, aby zapytanie SQL było łatwe do zmiany.

Umieść je w osobnym pliku lub module, np.:

```text
sql_queries.py
```

Przykładowe zapytanie:

```sql
SELECT TOP (100)
    id,
    order_number,
    customer_name,
    product_name,
    quantity,
    created_at
FROM dbo.orders
WHERE zello_notified = 0
ORDER BY id ASC;
```

Po poprawnym wysłaniu wiadomości:

```sql
UPDATE dbo.orders
SET
    zello_notified = 1,
    zello_notified_at = SYSUTCDATETIME()
WHERE id = ?;
```

Jednak nie zakładaj, że tabela użytkownika rzeczywiście posiada `zello_notified`.

Zaprojektuj dwie możliwości.

### Tryb A — flaga w tabeli orders

Jeśli tabela zawiera:

```text
zello_notified
zello_notified_at
```

korzystaj z niej.

### Tryb B — osobna tabela śledząca

Preferowane rozwiązanie produkcyjne:

```sql
CREATE TABLE dbo.zello_notifications (
    id BIGINT IDENTITY(1,1) PRIMARY KEY,
    source_type NVARCHAR(100) NOT NULL,
    source_id BIGINT NOT NULL,
    channel NVARCHAR(255) NOT NULL,
    status NVARCHAR(30) NOT NULL,
    attempts INT NOT NULL DEFAULT 0,
    last_error NVARCHAR(2000) NULL,
    created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    sent_at DATETIME2 NULL,

    CONSTRAINT UQ_zello_notifications
        UNIQUE(source_type, source_id, channel)
);
```

Statusy:

```text
pending
sending
sent
error
```

Mechanizm ma uniemożliwiać wysłanie tej samej informacji kilka razy.

Preferuję tabelę `zello_notifications`, ponieważ nie chcę wymuszać zmian w istniejących tabelach systemu ERP.

## Bardzo ważne — duplikaty

System musi być odporny na:

- restart aplikacji,
- restart Linuxa,
- zerwanie internetu,
- zerwanie WebSocket,
- restart SQL Server,
- timeout,
- chwilową niedostępność Zello.

Nigdy nie wysyłaj świadomie tego samego zamówienia kilka razy.

Zaprojektuj trwały mechanizm idempotencji.

Kluczem może być:

```text
source_type + source_id + channel
```

np.:

```text
order + 15231 + Magazyn
```

Jeżeli istnieje rekord ze statusem `sent`, nie wysyłaj go ponownie.

## Ważna uwaga o gwarancji dostarczenia

Nie udawaj gwarancji exactly-once pomiędzy dwoma niezależnymi systemami.

Jeśli Zello zaakceptuje wiadomość, ale aplikacja zakończy działanie przed zapisaniem statusu `sent` w SQL, teoretycznie może dojść do powtórzenia.

Zaprojektuj rozwiązanie typu at-least-once + idempotencja po stronie aplikacji i maksymalnie ogranicz okno wystąpienia duplikatu.

Opisz ten przypadek w README.

## WebSocket Zello

Połączenie powinno działać przez cały czas.

Zaimplementuj klasę:

```text
ZelloClient
```

Odpowiedzialną za:

- nawiązanie połączenia,
- logowanie,
- oczekiwanie na potwierdzenie logowania,
- sprawdzenie statusu kanału,
- generowanie kolejnych `seq`,
- wysyłanie wiadomości,
- oczekiwanie na odpowiedź odpowiadającą danemu `seq`,
- timeout odpowiedzi,
- reconnect,
- exponential backoff,
- obsługę utraty połączenia,
- bezpieczne ponowne logowanie.

Przykładowy backoff:

```text
1 s
2 s
5 s
10 s
30 s
60 s
```

Maksymalnie 60 sekund.

Po odzyskaniu połączenia aplikacja ma automatycznie kontynuować działanie.

Biblioteka WebSocket powinna poprawnie obsługiwać Ping/Pong Zello.

## Kanał

Nie wysyłaj wiadomości natychmiast po samym `logon`.

Poczekaj aż Zello zgłosi:

```json
{
  "command": "on_channel_status",
  "channel": "...",
  "status": "online",
  "texting_supported": true
}
```

Jeżeli:

```text
texting_supported = false
```

zaloguj błąd i nie oznaczaj powiadomienia jako wysłanego.

## Architektura aplikacji

Proponowana struktura:

```text
zello-sql-notifier/
├── app/
│   ├── __init__.py
│   ├── main.py
│   ├── config.py
│   ├── database.py
│   ├── repository.py
│   ├── zello_client.py
│   ├── notifier.py
│   ├── formatter.py
│   └── models.py
│
├── sql/
│   ├── create_notification_table.sql
│   └── example_query.sql
│
├── tests/
│   ├── test_formatter.py
│   ├── test_repository.py
│   └── test_zello_client.py
│
├── .env.example
├── .gitignore
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── zello-notifier.service
└── README.md
```

Nie twórz całej logiki w jednym pliku.

## Database

Przygotuj klasę/repository odpowiedzialne za:

```text
get_new_orders()
reserve_notification()
mark_notification_sent()
mark_notification_error()
```

Używaj parametryzowanych zapytań SQL.

Nigdy nie składaj SQL przez:

```python
f"SELECT ... {value}"
```

dla wartości pochodzących z danych.

Obsłuż automatyczny reconnect do MSSQL.

## Polling

Domyślnie sprawdzaj SQL co:

```text
2 sekundy
```

ale wartość musi pochodzić z:

```text
POLL_INTERVAL_SECONDS
```

Jeżeli nie ma żadnych nowych rekordów:

```text
sleep(POLL_INTERVAL_SECONDS)
```

Nie wykonuj busy-loop.

Pobieraj rekordy partiami, np.:

```text
100
```

Nie pobieraj za każdym razem całej tabeli.

## Formatowanie wiadomości

Stwórz oddzielny:

```text
formatter.py
```

Przykład:

```python
def format_order_message(order) -> str:
    ...
```

Format wiadomości powinien być łatwy do późniejszej zmiany.

Przykład:

```text
🔔 NOWE ZAMÓWIENIE

Numer: {order_number}
Klient: {customer_name}
Produkt: {product_name}
Ilość: {quantity}
```

Obsłuż wartości `NULL`.

Nie pozwól, aby `NULL` powodował wyjątek podczas formatowania.

## Kolejność

Proces dla jednego zamówienia:

```text
1. Znajdź nowe zamówienie.
2. Sprawdź tabelę zello_notifications.
3. Jeśli notification już ma status sent → pomiń.
4. Utwórz notification jako pending.
5. Przygotuj wiadomość.
6. Upewnij się, że WebSocket Zello jest online.
7. Wyślij send_text_message.
8. Poczekaj na odpowiedź Zello success=true.
9. Zapisz sent + sent_at.
10. Przejdź do kolejnego rekordu.
```

Jeżeli wysłanie się nie powiedzie:

```text
attempts += 1
last_error = treść błędu
status = error
```

Następnie rekord może zostać ponowiony.

Zaimplementuj limit/przerwę pomiędzy próbami.

## Retry

Przykład:

```text
1 próba
5 sekund
15 sekund
30 sekund
60 sekund
5 minut
```

Nie wykonuj nieskończonego retry kilka razy na sekundę.

Dodaj konfigurację:

```env
MAX_RETRY_ATTEMPTS=20
```

Po przekroczeniu liczby prób pozostaw rekord jako `error` i bardzo wyraźnie zapisz to w logu.

## Logowanie

Użyj standardowego `logging`.

Logi powinny wyglądać mniej więcej:

```text
2026-08-18 13:14:10 INFO Connected to MSSQL
2026-08-18 13:14:11 INFO Connected to Zello
2026-08-18 13:14:11 INFO Channel Magazyn online
2026-08-18 13:14:15 INFO Found new order id=15231
2026-08-18 13:14:15 INFO Sending notification order=15231
2026-08-18 13:14:16 INFO Zello confirmed notification order=15231
```

Błędy:

```text
ERROR Zello connection lost
WARNING Reconnecting to Zello in 5 seconds
ERROR MSSQL connection failed
```

Nigdy nie zapisuj w logach:

```text
MSSQL_PASSWORD
ZELLO_PASSWORD
```

## Shutdown

Obsłuż:

```text
SIGTERM
SIGINT
```

Program ma wykonać graceful shutdown:

- zakończyć pętlę,
- zamknąć WebSocket,
- zamknąć connection MSSQL,
- zakończyć proces.

Jest to konieczne do poprawnego działania pod `systemd` i Dockerem.

## Healthcheck

Dodaj prosty mechanizm statusu aplikacji.

Może to być mały HTTP endpoint:

```text
GET /health
```

odpowiadający:

```json
{
  "status": "ok",
  "database": "connected",
  "zello": "connected",
  "channel": "online"
}
```

Port:

```env
HEALTH_PORT=8080
```

Jeżeli nie chcesz dodawać dużego frameworka, użyj lekkiego rozwiązania.

## Docker

Przygotuj:

```text
Dockerfile
docker-compose.yml
```

Kontener musi zawierać Microsoft ODBC Driver 18 for SQL Server.

`docker-compose.yml`:

```yaml
services:
  zello-notifier:
    build: .
    restart: unless-stopped
    env_file:
      - .env
```

Nie zapisuj `.env` do obrazu Dockera.

## systemd

Oprócz Dockera przygotuj:

```text
zello-notifier.service
```

Przykładowa lokalizacja aplikacji:

```text
/opt/zello-notifier
```

Serwis powinien posiadać:

```text
Restart=always
RestartSec=5
```

Uruchomienie aplikacji nie powinno wymagać użytkownika root.

Przygotuj przykładowego użytkownika:

```text
zello-notifier
```

## Bezpieczeństwo

Zadbaj o:

- brak sekretów w repozytorium,
- `.env` z prawami 600,
- użytkownika MSSQL z minimalnymi uprawnieniami,
- osobnego użytkownika Zello dla bota,
- brak logowania haseł,
- parametryzowane SQL,
- TLS dla WebSocket,
- ograniczone uprawnienia kontenera/procesu.

Dla użytkownika MSSQL przygotuj listę minimalnych wymaganych uprawnień.

Nie używaj konta `sa`.

## Test mode

Dodaj:

```env
DRY_RUN=false
```

Jeżeli:

```text
DRY_RUN=true
```

aplikacja:

- odczytuje rekordy,
- formatuje wiadomość,
- pokazuje ją w logu,
- NIE wysyła jej do Zello,
- NIE oznacza produkcyjnego rekordu jako wysłany.

Ma to pozwolić przetestować zapytanie SQL bez wysyłania komunikatów na prawdziwy kanał.

## Test Zello

Dodaj możliwość ręcznego testu:

```bash
python -m app.main --test-zello
```

Polecenie powinno:

1. połączyć się z Zello,
2. zalogować,
3. poczekać na online kanału,
4. wysłać:

```text
Test połączenia SQL → Zello
```

5. sprawdzić `success=true`,
6. zakończyć się kodem 0.

Jeżeli test się nie powiedzie, zakończ kodem różnym od 0.

## Test MSSQL

Dodaj:

```bash
python -m app.main --test-db
```

Ma sprawdzić połączenie z bazą i wykonać:

```sql
SELECT 1;
```

Nie wysyła niczego do Zello.

## Test pojedynczej wiadomości

Dodaj:

```bash
python -m app.main --send-test "Test z serwera"
```

Ma wysłać wskazany tekst do skonfigurowanego kanału i zakończyć działanie.

## README

README musi zawierać kompletną instrukcję instalacji na Ubuntu/Debian Linux:

### Instalacja bez Dockera

1. instalacja Python 3.12,
2. instalacja Microsoft ODBC Driver 18,
3. utworzenie virtualenv,
4. instalacja requirements,
5. utworzenie `.env`,
6. test MSSQL,
7. test Zello,
8. uruchomienie aplikacji,
9. instalacja service systemd,
10. sprawdzanie logów.

Uwzględnij polecenia:

```bash
systemctl status zello-notifier
journalctl -u zello-notifier -f
```

### Instalacja Docker

Podaj:

```bash
docker compose build
docker compose up -d
docker compose logs -f
```

## Pierwsze uruchomienie

Bardzo ważne:

Nie chcę, aby po pierwszym uruchomieniu aplikacja wysłała kilka tysięcy historycznych zamówień.

Dodaj mechanizm:

```env
PROCESS_EXISTING_ON_FIRST_RUN=false
```

Jeżeli jest `false`, przy pierwszym starcie aplikacja ma ustawić punkt startowy na aktualne najwyższe ID zamówienia i obserwować dopiero rekordy utworzone później.

Przykład:

```sql
SELECT MAX(id)
FROM dbo.orders;
```

Zapisz ten checkpoint trwale w SQL.

Nie przechowuj go tylko w pamięci lub lokalnym pliku.

## Konfiguracja zapytania

Ponieważ nie znam jeszcze rzeczywistej struktury tabeli zamówień, przygotuj kod z przykładowym adapterem:

```python
class OrderRepository:
    def get_orders_after(self, last_id: int):
        ...
```

oraz jasno oznacz miejsce:

```text
TODO: CUSTOMIZE ORDER QUERY
```

Tak, aby później wystarczyło podmienić konkretne:

```text
nazwę tabeli
kolumnę ID
numer zamówienia
klienta
produkt
ilość
```

bez zmieniania całej aplikacji.

## Typy danych

Nie zakładaj, że numer zamówienia jest integerem.

Rozróżnij:

```text
database ID
order_number
```

Przykład:

```text
id = 15231
order_number = "ZAM/2026/08/00123"
```

Do checkpointu używaj stabilnego rosnącego `id`, a w wiadomości pokazuj `order_number`.

## Obsługa wielu procesów

Załóż, że normalnie działa jedna instancja aplikacji.

Mimo tego tabela `zello_notifications` powinna mieć UNIQUE constraint zapobiegający utworzeniu dwóch notification dla tego samego:

```text
source_type
source_id
channel
```

Obsłuż konflikt UNIQUE jako informację, że inny worker już utworzył notification.

## Kod

Kod powinien:

- mieć type hints,
- być czytelny,
- nie być nadmiernie skomplikowany,
- korzystać z async tam, gdzie ma to sens dla WebSocket,
- posiadać komentarze tylko tam, gdzie rzeczywiście coś wyjaśniają,
- nie używać globalnych mutable singletonów,
- mieć sensowną obsługę wyjątków.

## requirements.txt

Użyj stabilnych, aktualnych bibliotek, między innymi odpowiedników:

```text
pyodbc
websockets
python-dotenv
```

Jeżeli dodasz bibliotekę do healthcheck HTTP lub konfiguracji, uzasadnij ją.

Nie dodawaj dużych frameworków bez potrzeby.

## Testy

Dodaj testy jednostkowe minimum dla:

- formatowania wiadomości,
- deduplikacji,
- obsługi odpowiedzi `success=true`,
- obsługi `success=false`,
- timeout Zello,
- reconnect,
- NULL w danych zamówienia.

WebSocket Zello i MSSQL w testach jednostkowych mockuj.

## Etap pracy

Najpierw:

1. przeanalizuj wymagania,
2. przygotuj strukturę projektu,
3. napisz kod,
4. dodaj SQL,
5. dodaj Docker,
6. dodaj systemd,
7. dodaj testy,
8. uruchom testy,
9. popraw błędy,
10. przygotuj README.

Nie zatrzymuj się na samym szkielecie projektu.

Wygeneruj kompletne pliki.

## Na końcu odpowiedzi pokaż

1. strukturę katalogów,
2. listę utworzonych plików,
3. wynik testów,
4. dokładne polecenia potrzebne do uruchomienia,
5. które miejsca muszę zmienić po poznaniu struktury mojej tabeli MSSQL,
6. przykładowy `.env`,
7. przykładowe zapytanie SQL.

## Kryterium gotowości

Projekt uznaj za gotowy dopiero jeśli można wykonać:

```bash
cp .env.example .env
nano .env

python -m app.main --test-db
python -m app.main --test-zello
python -m app.main
```

i po dodaniu nowego rekordu do obserwowanej tabeli program automatycznie wyśle dokładnie sformatowaną wiadomość na skonfigurowany kanał Zello.