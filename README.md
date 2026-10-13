# Żółta Pinezka RMF FM – automatyczny nasłuch

Skrypt słucha RMF FM w chmurze (GitHub Actions, za darmo), zamienia mowę na tekst
(faster-whisper, model `small`, po polsku) i wysyła powiadomienie na telefon (ntfy):

| Kiedy (czas polski) | Co dostajesz |
|---|---|
| 13:50 | „Pinezka: nasłuch start” – znak, że wszystko działa |
| 14:00–14:30, sygnał „Żółta Pinezka RMF – 3,2,1 start!” | **ALARM** (priorytet max) z godziną końca 30-minutowego okna (PL i UK) |
| gdy w radiu padnie słowo „hasło” | **ALARM „HASŁO?”** + transkrypcja przez kolejne 2 min + **nagranie MP3** (~70 s) |
| 17:05 | „Pinezka: koniec” z podsumowaniem |

> **Ważne z regulaminu:** sygnał startu leci 14:00–14:30, ale **hasło jest podawane
> między 15:00 a 17:00**, a telefony od RMF są 15:00–18:00. Hasło trzeba podać
> **w dokładnie tej samej formie gramatycznej** – dlatego dostajesz nagranie, nie tylko tekst
> (transkrypcja może pomylić końcówkę). Konkurs trwa do **23.10.2026**; potem skrypt nic nie robi.
> Godziny w Wielkiej Brytanii: zawsze **o 1 godzinę wcześniej** (sygnał 13:00–13:30 UK, hasło 14:00–16:00 UK).

## Konfiguracja krok po kroku (ok. 15 min)

### 1. Aplikacja ntfy na telefonie
1. Zainstaluj **ntfy** (Google Play / App Store – darmowa).
2. Wymyśl długą, trudną do zgadnięcia nazwę tematu, np. `pinezka-sj-8f3k29xq`
   (temat w ntfy.sh jest publiczny dla każdego, kto zna nazwę – dlatego ma być losowa).
3. W aplikacji: **+** → *Subscribe to topic* → wpisz tę nazwę → *Subscribe*.
4. Android: w ustawieniach tematu włącz *Instant delivery* i pozwól na alerty o
   najwyższym priorytecie (żeby alarm dzwonił także w trybie cichym).

### 2. Repozytorium na GitHubie
1. Zaloguj się na github.com → **New repository**.
2. Nazwa np. `pinezka`, zaznacz **Public** (repozytoria publiczne mają darmowe minuty
   Actions bez limitu i maszyny 4-rdzeniowe). → *Create repository*.
3. Kliknij *uploading an existing file* i przeciągnij pliki: `pinezka.py`,
   `requirements.txt`, `README.md`. → *Commit changes*.
4. Plik workflow musi leżeć w folderze `.github/workflows/`. Najprościej:
   *Add file → Create new file*, w polu nazwy wpisz dokładnie
   `.github/workflows/pinezka.yml` (ukośniki same utworzą foldery), wklej zawartość
   pliku `pinezka.yml` → *Commit changes*.

### 3. Sekret z nazwą tematu
1. W repo: **Settings → Secrets and variables → Actions → New repository secret**.
2. Name: `NTFY_TOPIC`, Secret: nazwa tematu z kroku 1 (np. `pinezka-sj-8f3k29xq`). → *Add secret*.

### 4. Test ręczny (zrób od razu!)
1. Zakładka **Actions** → jeśli pyta, kliknij *I understand… enable workflows*.
2. Po lewej **Zolta Pinezka listener** → **Run workflow** → zostaw `5` minut i model `small` → *Run workflow*.
3. Po ok. 2–4 min (instalacja + pobranie modelu) na telefonie powinny przyjść:
   „nasłuch start”, 2× „TEST: transkrypcja działa” z tekstem z radia, a na końcu „koniec”.
4. W powiadomieniu „koniec” jest **średni czas przetwarzania** – musi być wyraźnie
   **poniżej 1.00** (np. 0.3x). Jeśli byłby ≥ 0.9, uruchom ponownie z modelem `base`
   i zmień domyślny model w pliku `pinezka.yml` (pole `default: "small"` i `|| 'small'`).
5. Jeśli coś nie działa: kliknij w uruchomienie → *listen* → przeczytaj log.
   Transkrypcja całego nasłuchu jest też do pobrania w sekcji *Artifacts* na dole strony.

### 5. Gotowe
Od teraz w dni robocze workflow startuje sam (ok. 13:20 czasu polskiego, nawet jeśli
GitHub spóźni się 30 min, zdąży przed 13:50). Zmiana czasu 25.10 jest obsłużona automatycznie.

## Jak to działa i ograniczenia
- Stream: oficjalne serwery RMF (`rsXXX-krk.rmfstream.pl/rmf_fm`, MP3) z listą zapasowych;
  przy zerwaniu połączenia skrypt łączy się ponownie i ostrzega, jeśli cisza trwa > 90 s.
- Stream internetowy ma zwykle **kilka–kilkanaście sekund opóźnienia** względem FM,
  a transkrypcja dokłada ~15–25 s. Na 30-minutowe okno to bez znaczenia.
- **Nie testowano na żywym streamie** w trakcie tworzenia – zrób test z kroku 4.
- Zaplanowane zadania GitHuba czasem się spóźniają, a bardzo rzadko w ogóle nie
  startują. Dlatego są dwa harmonogramy (główny + zapasowy) i powiadomienie „nasłuch start”:
  **jeśli o 14:00 czasu PL (13:00 UK) go nie masz – włącz radio sam.**
- Słowo „hasło” pada też w reklamach itp. – lepiej kilka fałszywych alarmów niż przegapić.
- To narzędzie tylko powiadamia. Zdjęcie wysyłasz sam (apka RMF FM lub rmf.fm), a hasło
  podajesz sam przez telefon – zgodnie z regulaminem zgłoszenie robi uczestnik.
- Regulamin nie zabrania nagrywania ani narzędzi tego typu, ale kategorie zgłoszenia
  (zdjęcie „jak słuchasz RMF FM”) i ocena jury się nie zmieniają – wystarczy, że słuchasz przez apkę.

## Przydatne komendy (opcjonalnie, na komputerze z Pythonem i ffmpeg)
```
pip install -r requirements.txt
python pinezka.py --selftest          # test wykrywania słów bez internetu
NTFY_TOPIC=twoj-temat python pinezka.py --test 3   # słuchaj teraz 3 minuty
```
