%global debug_package %{nil}
%global __os_install_post %{nil}
%global __arch_install_post %{nil}
%global _unitdir /usr/lib/systemd/system
%global _licensedir /usr/share/licenses

Name:           naps3
Version:        0.8
Release:        1.red80
Summary:        Сканирование документов через SANE и eSCL
License:        MIT AND BSD-2-Clause
Source0:        %{name}-%{version}.tar.gz

Requires:       python3
Requires:       python3-gobject
Requires:       gtk3
Requires:       python3-pillow
Requires:       sane-backends
Requires:       sane-airscan
Requires:       ipp-usb
Requires:       poppler-utils
Requires(post): systemd
Requires(postun): systemd

%description
GTK-приложение для USB- и сетевого сканирования со стекла и автоподатчика,
ручного и аппаратного дуплекса, изменения порядка страниц и сохранения PDF
или изображений. Восстановление остаётся привязано к выбранному МФУ, а файлы
при экспорте защищены временной записью и откатом. Пакет также содержит
исправленный транспорт ipp-usb для HP LaserJet Pro M428/M429.

%prep
%setup -q

%build

%install
rm -rf %{buildroot}

install -Dpm0755 naps3.py \
    %{buildroot}%{_libexecdir}/naps3/naps3.py
install -Dpm0644 windows_backend.py \
    %{buildroot}%{_libexecdir}/naps3/windows_backend.py
install -Dpm0755 ipp-usb-naps3 \
    %{buildroot}%{_libexecdir}/naps3/ipp-usb-naps3
install -Dpm0644 ipp-usb.conf \
    %{buildroot}%{_libexecdir}/naps3/ipp-usb-conf/ipp-usb.conf
install -Dpm0644 ipp-usb-m428.conf \
    %{buildroot}%{_libexecdir}/naps3/ipp-usb-quirks/90-naps3-hp-m428.conf

install -Dpm0755 packaging/naps3-launcher \
    %{buildroot}%{_bindir}/naps3
install -Dpm0644 packaging/ru.redos.NAPS3.desktop \
    %{buildroot}%{_datadir}/applications/ru.redos.NAPS3.desktop
install -Dpm0644 naps3.svg \
    %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/naps3.svg
install -Dpm0644 naps3.png \
    %{buildroot}%{_datadir}/icons/hicolor/256x256/apps/naps3.png

install -Dpm0644 packaging/ipp-usb-naps3-rpm.service.conf \
    %{buildroot}%{_sysconfdir}/systemd/system/ipp-usb.service.d/90-naps3-m428.conf
install -Dpm0644 README.txt \
    %{buildroot}%{_docdir}/%{name}/README.txt
install -Dpm0644 RPM_INSTALL.txt \
    %{buildroot}%{_docdir}/%{name}/RPM_INSTALL.txt
install -Dpm0644 LICENSE \
    %{buildroot}%{_licensedir}/%{name}/LICENSE
install -Dpm0644 LICENSE.ipp-usb \
    %{buildroot}%{_licensedir}/%{name}/LICENSE.ipp-usb

%check
/usr/bin/python3 -m py_compile naps3.py windows_backend.py
/usr/bin/bash -n packaging/naps3-launcher

%post
/usr/bin/systemctl daemon-reload >/dev/null 2>&1 || :
/usr/bin/systemctl restart ipp-usb.service >/dev/null 2>&1 || :
/usr/bin/update-desktop-database %{_datadir}/applications >/dev/null 2>&1 || :
/usr/bin/gtk-update-icon-cache -f %{_datadir}/icons/hicolor >/dev/null 2>&1 || :
exit 0

%postun
/usr/bin/systemctl daemon-reload >/dev/null 2>&1 || :
if [ "$1" -eq 0 ]; then
    /usr/bin/systemctl restart ipp-usb.service >/dev/null 2>&1 || :
fi
/usr/bin/update-desktop-database %{_datadir}/applications >/dev/null 2>&1 || :
/usr/bin/gtk-update-icon-cache -f %{_datadir}/icons/hicolor >/dev/null 2>&1 || :
exit 0

%files
%{_bindir}/naps3
%dir %{_libexecdir}/naps3
%{_libexecdir}/naps3/naps3.py
%{_libexecdir}/naps3/windows_backend.py
%{_libexecdir}/naps3/ipp-usb-naps3
%dir %{_libexecdir}/naps3/ipp-usb-conf
%{_libexecdir}/naps3/ipp-usb-conf/ipp-usb.conf
%dir %{_libexecdir}/naps3/ipp-usb-quirks
%{_libexecdir}/naps3/ipp-usb-quirks/90-naps3-hp-m428.conf
%{_datadir}/applications/ru.redos.NAPS3.desktop
%{_datadir}/icons/hicolor/scalable/apps/naps3.svg
%{_datadir}/icons/hicolor/256x256/apps/naps3.png
%{_sysconfdir}/systemd/system/ipp-usb.service.d/90-naps3-m428.conf
%doc %{_docdir}/%{name}/README.txt
%doc %{_docdir}/%{name}/RPM_INSTALL.txt
%license %{_licensedir}/%{name}/LICENSE
%license %{_licensedir}/%{name}/LICENSE.ipp-usb

%changelog
* Tue Sep 08 2026 NAPS3 contributors <noreply@localhost> - 0.8-1.red80
- Добавлена отдельная сборка для Windows с системным backend WIA.
- Linux backend SANE/eSCL и существующий интерфейс сохранены.
- Импорт PDF использует встроенный pdftoppm в Windows-пакете.

* Tue Sep 08 2026 NAPS3 contributors <noreply@localhost> - 0.7-1.red80
- Восстановление соединения больше не переключает задание на другое МФУ.
- Изменения страниц блокируются во время сохранения.
- Результат записывается во временные файлы с проверкой и откатом замены.
- При закрытии предлагается сохранить несохранённый документ.

* Thu Jul 30 2026 NAPS3 contributors <noreply@localhost> - 0.6.7-1.red80
- В список форматов отдельных файлов добавлен PDF.
- Каждая страница сохраняется отдельным одностраничным PDF.
- PDF выбран форматом по умолчанию.

* Thu Jul 30 2026 NAPS3 contributors <noreply@localhost> - 0.6.6-2.red80
- Защищено обновление RPM от смешивания со старыми ручными скриптами.
- Восстанавливающее обновление для повреждённой ручным удалением установки.

* Thu Jul 30 2026 NAPS3 contributors <noreply@localhost> - 0.6.6-1.red80
- Отмена сканирования больше не оставляет зависший backend и занятый UI.
- Выделение страницы сохраняется после поворота и перестроения эскизов.

* Thu Jul 30 2026 NAPS3 contributors <noreply@localhost> - 0.6.5-1.red80
- Первый переносимый пакет для РЕД ОС 8 x86_64.
- Исправлен ZLP/backoff USB-транспорта HP M428/M429.
- Сохранено надёжное пакетное получение всех страниц АПД.
