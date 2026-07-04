# Design: wzbogacony schemat dla LLM (metadane, choices, wartości FK)

Data: 2026-07-04
Moduł: `djangoql/llm.py`, `djangoql/management/commands/djangoql_describe_schema_for_llm.py`
Dokumentacja: `docs/llm-schema.md`

## Cel

`describe_schema_for_llm` obecnie opisuje strukturę i składnię (typ, nullable,
operatory, przykłady, `suggested_values` tylko dla pól z `suggest_options`).
Brakuje **sygnału semantycznego**: do czego pole służy i jaka jest dziedzina
jego wartości. To najtańsza rzecz, która podnosi trafność generowanego DjangoQL.

Rozszerzamy opis o trzy aspekty, wszystkie **defensywne** (żaden brak metadanej
ani wyjątek DB nie może wywalić opisu schematu — wzorzec z `_field_options`).

## Aspekt 1 — Metadane nazewnicze (`label`, `help_text`)

Źródło: `field.model._meta.get_field(field.name)` (idiom już używany w
`schema.py:56`).

- `label` ← `verbose_name`. **Pomijamy, gdy równa się autogenerowanej
  domyślnej** Django, czyli `field.name` z `_`→spacja (porównanie
  case-insensitive po strip). Sens: emitować tylko labelki wnoszące treść,
  nie dublować nazwy technicznej.
- `help_text` ← emitowany, gdy niepusty (po strip).
- Dotyczy też relacji (`verbose_name` pola FK).
- Pola bez realnego pola modelu (`CountField`, `AggregateField`, brak
  `self.model`, `FieldDoesNotExist`) → cicho pomijane w `try/except`.

## Aspekt 2 — Choices zawsze

DjangoQL dopasowuje choices po **etykiecie** (`c[1]`), nie po wartości z bazy —
`get_lookup_value` (`schema.py:85-96`) tłumaczy etykietę na wartość DB, a
`get_options('')` (`schema.py:74`) już zwraca etykiety. Czyli istniejąca ścieżka
emituje poprawne stringi do zapytania.

- Pole, którego model field ma `choices` → **zawsze** emituje warianty,
  niezależnie od flagi `suggest_options` (choices są statyczne i darmowe —
  zero SQL, `super().get_options()` czyta je z `_meta`).
- Osobny klucz `choices` (zbiór **zamknięty**) zamiast `suggested_values`
  (otwarte podpowiedzi), plus `note`: wartość musi być jedną z wymienionych.
- Cap defensywny: stała `MAX_CHOICE_VALUES` (domyślnie 100).
- Istniejące `suggested_values` (distinct z DB dla pól tekstowych oflagowanych
  `suggest_options`) zostaje bez zmian — dla otwartych pól tekstowych.

## Aspekt 3 — Wartości FK (auto wg progu + `fk_options`)

W DjangoQL relacji nie porównuje się jako całości (`publisher = X` jest
nielegalne poza `= None`) — przechodzi się kropką: `publisher.name = "Manning"`.
Dlatego „wartości FK" znaczą: distinct-wartości pola identyfikującego modelu
docelowego, podane razem ze ścieżką traversalu.

### Sterowanie

- Globalny próg liczby rekordów: `describe_schema_for_llm(schema,
  max_fk_options=50)`; flaga `--max-fk-options` w komendzie zarządzającej.
  Domyślnie 50.
- Słownik na klasie schematu:

  ```python
  class MySchema(DjangoQLSchema):
      fk_options = {
          Book: {
              'publisher': 'name',              # publisher.name in (...)
              'author': ['last_name', 'skrot'], # wiele pól
              'category': '__str__',            # str(obj) jako przykłady
              'tag': False,                     # nigdy (zero SQL)
              'status': True,                   # wymuś, pole domyślne, ignoruj próg
          }
      }
  ```

### Znaczenie `spec` (wartość per relacja w `fk_options`)

| `spec`              | Zachowanie |
|---------------------|-----------|
| `'name'` (str)      | Emituj distinct-wartości `related.name` jako `match_field` + `suggested_values`; `note: "publisher.name = <value>"`. Podlega progowi. |
| `['a', 'b']` (list) | Wiele pól identyfikujących: `match_fields` + wartości per pole. Podlega progowi. |
| `'__str__'`         | `str(obj)` wierszy jako `related_examples` (przybliżone — gdy identyfikuje kombinacja pól, np. nazwa/skrót). Podlega progowi. |
| `False`             | Nigdy nie emituj. Zero zapytań DB. |
| `True`              | Wymuś emisję polem domyślnym, **ignorując próg**. |

### Tryb auto (relacja bez wpisu w `fk_options`)

- Policz rekordy modelu docelowego. Jeśli `count <= max_fk_options` → emituj
  polem domyślnym; powyżej → pomiń (bez wartości, sam traversal jak dziś).
- Heurystyka pola domyślnego: pole o nazwie `name` → pierwsze pole
  `CharField`/`TextField` → fallback `str(obj)` (jako `related_examples`).
- **Potwierdzona decyzja:** tryb auto jest włączony — każda relacja bez wpisu
  generuje 1× COUNT. Świadomy trade-off: wygoda kosztem N zapytań COUNT.

### Koszt i bezpieczeństwo

- Tryb auto: 1× COUNT na relację + 1× distinct na emitowaną relację, **przy
  generowaniu opisu** (operacja jednorazowa / cache'owalna, nie per-request).
- `False` = zero zapytań. Wszystkie dostępy do DB w `try/except` — brak wartości
  nigdy nie przerywa opisu schematu.
- Do progu pobieramy `[:max_fk_options + 1]`, by rozstrzygnąć „<= próg" jednym
  zapytaniem bez osobnego COUNT tam, gdzie to możliwe.

## Format wyjścia (przykład relacji)

```jsonc
"publisher": {
  "type": "relation",
  "relates_to": "core.Publisher",
  "nullable": true,
  "operators": ["= None", "!= None", "<relation>.<field> (traverse with a dot)"],
  "label": "Wydawca",
  "help_text": "Podmiot wydający książkę",
  "match_field": "name",
  "suggested_values": ["Manning", "O'Reilly", "PWN"],
  "note": "match by traversal: publisher.name = <value>"
}
```

Dla `spec='__str__'` lub fallbacku heurystyki: zamiast `match_field` +
`suggested_values` klucz `related_examples` + `note` o traversalu do pola
identyfikującego.

## Testy (TDD — testy przed implementacją)

Plik `test_project/core/tests/test_llm.py`. Przypadki:

- `label`/`help_text`: pole z jawnym `verbose_name`/`help_text`; pomijanie
  autogenerowanego `verbose_name`; pole bez modelu (custom) nie wywala opisu.
- choices: pole z choices emituje `choices` zawsze (bez `suggest_options`);
  wartości to etykiety; cap `MAX_CHOICE_VALUES`.
- FK auto: relacja poniżej progu emituje `match_field`+wartości; powyżej progu
  pomija; `max_fk_options` respektowany.
- `fk_options`: `'name'`, lista pól, `'__str__'`, `False`, `True` (ignoruje próg).
- defensywność: błąd DB / brak pola nie przerywa `describe_schema_for_llm`.

W razie potrzeby dołożymy modele/fixtury w `test_project/core` (model z choices,
model docelowy FK o małej i dużej kardynalności, pola z `verbose_name`/`help_text`).

## Dokumentacja użytkownika (twardy deliverable)

Rozszerzyć istniejące `docs/llm-schema.md`:

- Nowa sekcja o `label`/`help_text` w opisie pola (skąd pochodzą, kiedy
  pomijane).
- Sekcja o choices (zamknięty zbiór, emitowane etykiety).
- Sekcja o wartościach FK: `max_fk_options`, słownik `fk_options` z tabelą
  znaczeń `spec`, tryb auto + heurystyka pola domyślnego, uwaga o koszcie
  (N× COUNT) i o tym, jak wyłączyć (`False`).
- Zaktualizować przykładowy JSON wyjścia o nowe klucze.
- Zweryfikować `mkdocs build --strict` (bez orphanów/broken links).

## Poza zakresem (YAGNI)

- Konfigurowalny cap dla choices per pole (globalna stała wystarcza).
- Emisja wartości FK po pk / `object_reference` (semantyka pozostaje bez zmian).
- Cache wyników COUNT między wywołaniami (opis jest jednorazowy).
