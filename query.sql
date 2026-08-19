-- ============================================================================
-- ZAPYTANIE O ZAMÓWIENIA — edytuj TEN plik, nie kod!
-- ============================================================================
-- Bot wykonuje to zapytanie co POLL_INTERVAL sekund. Jeśli zwróci wiersz —
-- wysyła powiadomienie na kanał Zello (nawet jeśli to ten sam wiersz, co
-- w poprzednim pollingu — bot nie pamięta obsłużonych zamówień).
--
-- Wymagania:
--   * max 1 wiersz (TOP 1),
--   * co najmniej 1 kolumna = numer zamówienia pokazywany w wiadomości
--     (u Ciebie: OriginalNumber),
--   * opcjonalnie druga kolumna `id` (liczba) — tylko do logów; wtedy
--     kolejność: id, numer.
--
-- Własny warunek wpisz w WHERE (np. status = 'oczekuje'), a kolumnę
-- sortowania / warunku dostosuj do swojej tabeli.
-- ============================================================================

SELECT TOP(1)OriginalNumber
  FROM [SerwisKop_Magazyn].[Document].[Documents]
  WHERE DocumentType = 7
  AND DateCreatedUtc >= '2026-06-01'
  AND DocumentStatusText = 'new'
  AND SubType = 50