# Патченый движок как образ: и сборщик тайлов, и маршрутизатор, и инструмент порталов.
#
# ПОЧЕМУ ФАБРИКЕ КАРТ НУЖЕН НАШ ФОРК, А НЕ ghcr.io/valhalla/valhalla. Multi-region — это правка
# runtime, кроме ОДНОЙ строки, которая живёт в Mjolnir: NodeInfo::can_contract() отказывает
# kBorderControl. Без неё шорткат накрывает пограничный пост и прячет от поиска 600 секунд, а
# маршрут Кишинёв -> Бухарест уходит на коридор длиннее на 13 км. То есть тайлы, собранные
# стоковым образом, для нашей схемы непригодны, и это не вопрос версии — это вопрос сборщика.
#
# Сам формат .gph при этом НЕ менялся: регион живёт в битах 46..63 GraphId и никогда не
# достигает файла.
#
# Образ собирается один раз на тег движка и потом только скачивается. Пересобирать его на
# каждую страну значило бы платить сорок минут за то, что не меняется.

FROM ubuntu:24.04 AS builder

ARG ENGINE_REPO=https://github.com/slavonip/wedrive-valhalla.git
ARG ENGINE_REF=multi-region-runtime-v2

ENV DEBIAN_FRONTEND=noninteractive
ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH
ENV LD_LIBRARY_PATH=/usr/local/lib:/lib/x86_64-linux-gnu:/usr/lib/x86_64-linux-gnu

RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates sudo python3 \
 && rm -rf /var/lib/apt/lists/*

# --recurse-submodules обязателен: third_party копируется в upstream-образ, и обычного клона
# не хватает — сборка падает поздно, на отсутствующих заголовках.
RUN git clone --depth 1 --branch "${ENGINE_REF}" --recurse-submodules --shallow-submodules \
      "${ENGINE_REPO}" /src/valhalla

WORKDIR /src/valhalla
RUN bash ./scripts/install-linux-deps.sh && rm -rf /var/lib/apt/lists/*

# Патчи применяются ИЗ РЕПОЗИТОРИЯ ДВИЖКА, а не копируются сюда: иначе в фабрике карт завелась
# бы вторая копия, которая тихо разойдётся с первой.
RUN bash wedrive/apply-patches.sh /src/valhalla | tail -25

RUN cmake -B build -DCMAKE_BUILD_TYPE=Release \
      -DENABLE_TESTS=Off -DENABLE_SINGLE_FILES_WERROR=Off -DENABLE_SERVICES=On \
 && make -C build -j"$(nproc)" \
 && make -C build install \
 && ldconfig

# Инструменты фабрики из того же дерева: таблица порталов и проверка инварианта GraphId.
RUN cd wedrive/tools \
 && g++ -std=c++20 -O2 portal_border.cc -o /usr/local/bin/portal_border \
      $(pkg-config --cflags --libs libvalhalla) \
 && g++ -std=c++20 -O2 gid_test.cc -o /usr/local/bin/gid_test \
      $(pkg-config --cflags --libs libvalhalla) \
 && cp /src/valhalla/wedrive/tools/make-manifest.py /usr/local/bin/make-manifest.py 2>/dev/null || true

# FRONTIER: the border nodes of ONE country graph (scripts/regional-frontier.py). It lives in this
# repository, not in the engine's, because it is a factory tool that only READS tiles: it adds a
# binary to the image and changes nothing the engine does — engine_sha, and with it every
# country's rebuild decision, stay the same.
COPY docker/tools/frontier_dump.cc /src/factory-tools/frontier_dump.cc
RUN g++ -std=c++20 -O2 /src/factory-tools/frontier_dump.cc -o /usr/local/bin/frontier_dump \
      $(pkg-config --cflags --libs libvalhalla)

# Отпечаток движка внутри образа: по нему манифест называет версию, которой собран пакет.
RUN echo "${ENGINE_REF}" > /etc/wedrive-engine-ref \
 && git -C /src/valhalla rev-parse HEAD > /etc/wedrive-engine-sha

# РАБОЧИЙ ОБРАЗ СТРОИТСЯ НА СТОКОВОМ VALHALLA ТОЙ ЖЕ ВЕРСИИ, а не на голой ubuntu.
#
# Перечислять runtime-библиотеки руками — значит угадывать имена пакетов для конкретного
# выпуска дистрибутива (libgdal34, libspatialite8t64 и прочее), и ошибка вскроется не при
# сборке образа, а при первом запуске. Стоковый образ 3.6.3 — ровно то окружение, в котором
# наш форк и собран; мы заменяем в нём только бинарники на патченые.
FROM ghcr.io/valhalla/valhalla:3.6.3
ENV DEBIAN_FRONTEND=noninteractive
ENV LD_LIBRARY_PATH=/usr/local/lib:/lib/x86_64-linux-gnu:/usr/lib/x86_64-linux-gnu
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
      osmium-tool jq curl ca-certificates python3 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=builder /usr/local/bin/ /usr/local/bin/
COPY --from=builder /usr/local/lib/ /usr/local/lib/
COPY --from=builder /etc/wedrive-engine-ref /etc/wedrive-engine-ref
COPY --from=builder /etc/wedrive-engine-sha /etc/wedrive-engine-sha
RUN ldconfig

# Образ обязан уметь то, ради чего он существует. Если инвариант GraphId сломан, дальше собирать
# карты бессмысленно — падаем здесь, а не через сорок минут на тайлах.
RUN gid_test > /dev/null && echo "инвариант GraphId: ok"
RUN valhalla_build_tiles --help > /dev/null 2>&1 || true
RUN test -x /usr/local/bin/portal_border || (echo "нет portal_border" >&2; exit 1)
RUN test -x /usr/local/bin/frontier_dump || (echo "нет frontier_dump" >&2; exit 1)
