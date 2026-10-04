# Proxmox BetterDockers

Dodatek do Proxmox VE, który dodaje przycisk **Utwórz Docker**, osobny kreator oraz ikonę Dockera w drzewie maszyn. Skrypt startowy: `start.sh`.

Każda maszyna Docker to **osobny nieuprzywilejowany LXC z Debianem, Docker Engine i Compose v2**, własnym VMID, interfejsem sieciowym i IP. Backend Proxmox nadal traktuje ją jako LXC. Dodatek nie tworzy nowego typu w rdzeniu platformy; dzięki temu działają istniejące operacje start/stop, konsola, zasoby, sieć, backup i usuwanie.

## Instalacja

Uruchom jako **root na hoście Proxmox VE** — instalacja jedną komendą:

```bash
curl -fsSL https://github.com/GDLev/Proxmox-BetterDockers/-/raw/HEAD/install.sh | bash
```

Skrypt `install.sh` pobiera archiwum domyślnej gałęzi do katalogu tymczasowego i uruchamia `start.sh`, który wykonuje `apt-get update` oraz instaluje zależności (`python3`, `ca-certificates`). Po zakończeniu usuwa pobraną kopię projektu. Git nie jest wymagany; do pobrania potrzebne są `curl` i `tar`. Ponowne uruchomienie tej samej komendy pobiera aktualny projekt i ponownie instaluje integrację.

Adres skryptu i archiwum musi być dostępny bez logowania. Dla prywatnego repozytorium pobierz projekt po uwierzytelnieniu i uruchom lokalne `bash start.sh install`.

Jeśli masz już kopię projektu, uruchom z jego katalogu:

```bash
bash start.sh check
bash start.sh install
```

Wymagane: Proxmox VE 8 lub 9 i standardowe narzędzia PVE. Python 3 jest instalowany automatycznie; tryb `check` wymaga jego wcześniejszej dostępności. Instalator sprawdza strukturę plików konkretnej wersji. W klastrze zainstaluj dodatek **na każdym węźle**, również na węźle udostępniającym interfejs WWW.

Instalacja restartuje `pvedaemon` i `pveproxy`. Nie restartuje maszyn. Kopie zmienianych plików trafiają do `/var/backups/betterdocker/`. Po instalacji odśwież przeglądarkę przez Ctrl+Shift+R i zaloguj się jako **root@pam**. Tworzenie przez dodatek jest obecnie dostępne wyłącznie dla root@pam; własny endpoint nie udostępnia wykonywania poleceń innym użytkownikom.

Przed utworzeniem maszyny pobierz w Proxmox standardowy szablon **Debian 12 lub 13** do magazynu z zawartością `vztmpl`. Nie wybieraj szablonu appliance ani backupu. Węzeł musi mieć magazyn obsługujący rootfs LXC; nowy gość potrzebuje dostępu do Internetu, DNS i repozytoriów Debian/Docker.

## Kreator

Kreator dziedziczy rzeczywiste ekrany Proxmox: ogólne, szablon, dyski, CPU, pamięć, sieć i DNS. Dodaje:

- **Docker Engine**: pojedynczy obraz, projekt Compose albo sam silnik; zmienne środowiskowe i polecenie jako JSON.
- **Porty**: lista mapowań TCP/UDP jako JSON. „host” to port na IP tej maszyny Docker.
- **Docker Compose**: edytor pełnego `compose.yaml`.
- **Potwierdzenie** i standardowy podgląd zadania PVE z postępem instalacji.

Przykład pojedynczego obrazu: `nginx:stable`, porty:

```json
[{"host":8080,"container":80,"protocol":"tcp"}]
```

Przykład Compose:

```yaml
services:
  web:
    image: nginx:stable
    restart: unless-stopped
    ports:
      - "8080:80"
    volumes:
      - web-data:/usr/share/nginx/html
volumes:
  web-data:
```

W trybie Compose konfiguracja portów, zmiennych i poleceń pochodzi z YAML. Pola tych ustawień dla pojedynczego obrazu są wtedy pomijane. Sam silnik nie wdraża aplikacji. Pojedynczy obraz jest zapisywany jako projekt Compose z `restart: unless-stopped`; w własnym YAML ustaw politykę restartu samodzielnie.

Dla maszyny wskaż bridge, np. `vmbr0`, oraz **DHCP albo własny unikalny IPv4/CIDR i bramę**. DHCP przydziela adres przez serwer Twojej sieci; dodatek nie jest serwerem DHCP ani menedżerem puli IP. Dla przewidywalnego adresu użyj rezerwacji DHCP albo konfiguracji statycznej. Po instalacji adres pojawia się w logu zadania.

Osobne IP otrzymuje każda **maszyna Docker**. Usługi jednego projektu Compose współdzielą IP maszyny i publikują różne porty; dodatek nie przydziela osobnych adresów LAN każdej usłudze Compose. Obraz, np. Nginx z mapowaniem powyżej, będzie dostępny pod `http://IP_MASZYNY:8080`.

## Obsługa i diagnostyka

Maszyny mają tag `betterdocker`. Ikona jest nadawana na podstawie tego tagu bez zmieniania natywnego typu zasobu. Nie usuwaj tagu, jeśli chcesz zachować ikonę. Konsola, zasoby, backup i pozostałe ekrany zarządzania są natywnymi ekranami LXC; osobny kreator dotyczy tworzenia maszyny.

Projekt Compose znajduje się **wewnątrz gościa** w `/opt/betterdocker/compose.yaml` z uprawnieniami 0600. Polecenia wewnątrz jego konsoli:

```bash
cd /opt/betterdocker
docker compose -p betterdocker ps
docker compose -p betterdocker logs --tail=100
docker compose -p betterdocker up -d
```

Tryb samego silnika nie tworzy pliku Compose. Edycja aplikacji po instalacji odbywa się przez konsolę i ten plik. Po błędzie tworzenia lub wdrożenia sprawdź log zadania. Utworzony LXC pozostaje na hoście do diagnostyki; dodatek nie usuwa jego danych automatycznie. Ponowne uruchomienie kreatora wymaga nowego wolnego VMID. Napraw istniejący projekt przez konsolę albo usuń nieudanego gościa przez standardowy interfejs Proxmox.

Backup obejmuje pliki zapisane na rootfs gościa, w tym dane Dockera i nazwane wolumeny. Zewnętrzne bind mounts i dane zdalne wymagają własnej konfiguracji backupu. Mapowania portów odnoszą się do gościa, a reguły sieciowe/firewall wymagają dostosowania do własnej sieci.

## Aktualizacje i usuwanie

Pakiety Proxmox mogą nadpisać dwa punkty integracji. Po aktualizacji `pve-manager` uruchom:

```bash
bash start.sh repair
```

`repair` działa tak samo jak instalacja, z kontrolą struktury i ponownym wpięciem dodatku. Nie ma automatycznego hooka modyfikującego system podczas aktualizacji. Odinstalowanie:

```bash
bash start.sh uninstall
```

Usuwa oznaczone fragmenty integracji i pliki dodatku. Nie przywraca starych wersji plików pakietów i nie usuwa maszyn. Istniejące maszyny pozostają zwykłymi LXC z działającym Dockerem.

## Walidacja i ograniczenia

Wykonane lokalnie: testy Python dla walidacji, generowania Compose, obsługi zadań i odwracalności patcha; kontrola składni Bash i JavaScript; próba patchowania aktualnych plików źródłowych Proxmox. **Pełny test instalacji, GUI i uruchomienia Dockera na żywym Proxmox pozostaje do wykonania.** Proxmox 8/9 to zakres wersji instalatora, a nie deklaracja przetestowania wszystkich wydań.

```bash
python3 -m unittest discover -s tests -v
bash -n install.sh start.sh scripts/docker-install.sh
node --check frontend/betterdocker.js
node tests/test_frontend.js
```

Integracja używa wewnętrznych klas ExtJS i API PVE, więc duże zmiany tych klas mogą wymagać aktualizacji dodatku. Docker w LXC zależy też od zgodności kernela, LXC, AppArmor i runtime Dockera. Dodatek włącza `nesting=1,keyctl=1`, zachowując nieuprzywilejowany LXC; nie wyłącza AppArmor. Proxmox rekomenduje VM dla aplikacyjnych kontenerów wymagających mocniejszej izolacji.

Źródła implementacji: [kreator LXC Proxmox](https://github.com/proxmox/pve-manager/blob/master/www/manager6/lxc/CreateWizard.js), [API węzłów](https://github.com/proxmox/pve-manager/blob/master/PVE/API2/Nodes.pm), [dokumentacja LXC](https://pve.proxmox.com/pve-docs/chapter-pct.html), [instalacja Docker na Debianie](https://docs.docker.com/engine/install/debian/).
