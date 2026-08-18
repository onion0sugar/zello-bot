Tu wrzuć plik dźwiękowy powiadomienia, np. `new_order.wav`
(nagranie głosowe, np. „Uwaga. Pojawiło się nowe zamówienie.").

Ścieżkę ustawiasz w `.env`:

    VOICE_FILE=audio/new_order.wav

Ścieżka względna liczy się od katalogu, z którego uruchamiasz bota:
- ręcznie z /opt/zello-bot  → /opt/zello-bot/audio/new_order.wav
- systemd (WorkingDirectory=/opt/zello-bot w zello-bot.service) → to samo

Możesz też podać ścieżkę bezwzględną, np. VOICE_FILE=/opt/zello-bot/audio/new_order.wav

Wymagania: zwykły plik WAV (dowolna częstotliwość/kanały — bot i tak
przekonwertuje go FFmpeg do PCM 16 kHz mono na starcie). Bot ładuje i koduje
plik raz, przy uruchomieniu — jeśli pliku brakuje, kończy pracę z jasnym
błędem.
