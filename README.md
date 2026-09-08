# NAPS3 для РЕД ОС

GTK-приложение для сканирования документов через USB и сеть. Поддерживает
стекло, автоподатчик, ручной и аппаратный дуплекс, импорт страниц и сохранение
в PDF, TIFF, PNG, JPEG, BMP и WebP.

Текущая версия — **0.7** для **РЕД ОС 8 x86_64**.

## Установка одной командой

Прямо из выпуска GitHub:

```bash
sudo dnf install -y https://github.com/Kostya5232/naps3-redos/releases/latest/download/naps3-latest.x86_64.rpm
```

Чтобы получать следующие версии обычной командой `dnf upgrade`, один раз
подключите бесплатный репозиторий:

```bash
sudo curl -fsSL https://kostya5232.github.io/naps3-redos/naps3.repo -o /etc/yum.repos.d/naps3.repo && sudo dnf install -y naps3
```

После этого обновление выполняется вместе с системой:

```bash
sudo dnf upgrade
```

Репозиторий пока публикуется без RPM-подписи (`gpgcheck=0`). Пакеты и метаданные
передаются по HTTPS, а GitHub Release дополнительно содержит SHA-256.

## Что изменилось в 0.7

- восстановление соединения остаётся привязано к выбранному МФУ;
- страницы нельзя изменить во время сохранения;
- экспорт сначала записывает и проверяет временные файлы, затем заменяет итоговые;
- при закрытии программа предлагает сохранить несохранённые страницы и изменения.

Полная история и описание возможностей находятся в [README.txt](README.txt),
а устройство исходного кода — в [PROGRAM_MAP.md](PROGRAM_MAP.md).

## Проверка и сборка

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
./build_rpm.sh
```

Для GTK smoke-теста нужны `python3-gobject`, GTK 3, Pillow и Xvfb:

```bash
xvfb-run -a env PYTHONPATH=. python3 tests/gtk_smoke.py
```

Теги вида `v0.7` запускают GitHub Actions: тесты, сборку RPM, создание выпуска и
обновление DNF-репозитория на GitHub Pages.

## Лицензии

Код приложения распространяется по лицензии MIT. Встроенный транспорт
`ipp-usb-naps3` распространяется по BSD 2-Clause; его лицензия находится в
[LICENSE.ipp-usb](LICENSE.ipp-usb).

