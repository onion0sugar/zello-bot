-- ============================================================================
-- ZAPYTANIE O ZAMÓWIENIA — edytuj TEN plik, nie kod!
-- ============================================================================
-- Bot wykonuje to zapytanie co POLL_INTERVAL sekund. Jeśli zwróci wiersz —
-- wysyła powiadomienie na kanał Zello (nawet jeśli to ten sam wiersz, co
-- w poprzednim pollingu — bot nie pamięta obsłużonych zamówień).
--
-- Wymagania:
--   * max 1 wiersz (TOP 1),
--   * kolumna `id`           — identyfikator (do logów),
--   * kolumna `order_number` — numer pokazywany w wiadomości.
--
-- Własny warunek wpisz w WHERE, np. status = 'oczekuje'.
-- ============================================================================

SELECT TOP 1
    id,
    order_number
FROM dbo.orders
WHERE id > 0
ORDER BY id ASC;
