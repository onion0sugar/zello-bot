-- ============================================================================
-- ZAPYTANIE O ZAMÓWIENIA — edytuj TEN plik, nie kod!
-- ============================================================================
-- Bot wykonuje to zapytanie co POLL_INTERVAL sekund i dostaje LISTĘ zamówień.
-- Każdy wiersz musi zawierać (nazwy kolumn — kolejność NIE ma znaczenia):
--   * OriginalNumber (lub OrderNumber)  — numer zamówienia,
--   * DocumentStatusText (lub Status)   — status: 'new' lub 'in_progress',
--   * ModifiedBy                        — kto obsługuje zamówienie,
--   * Id (opcjonalnie)                  — tylko do logów.
--
-- Zachowanie bota:
--   * jest >= 1 wiersz ze statusem 'new'  → powiadomienie do WSZYSTKICH
--     użytkowników z user_mapping.json MINUS ci, którzy mają zamówienie
--     ze statusem 'in_progress' (po kolumnie ModifiedBy),
--   * brak wierszy 'new'                  → brak powiadomienia.
--
-- WAŻNE: bez TOP(1) / LIMITU! Do wykluczania zajętych bot potrzebuje
-- CAŁEJ listy dzisiejszych zamówień, nie jednego wiersza.
-- ============================================================================

SELECT OriginalNumber,ModifiedBy,DocumentStatusText
  FROM [SerwisKop_Magazyn].[Document].[Documents]
  WHERE DocumentType = 7
  AND DateCreatedUtc >= CAST(GETDATE() AS DATE)
  AND DocumentStatusText IN ('new', 'in_progress')
  AND SubType = 50