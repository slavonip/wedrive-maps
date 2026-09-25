# -*- coding: utf-8 -*-
"""Регрессия на семантику доступа для поиска коннекторов.

Существует потому, что пропуск ОДНОГО тега стоил всех переправ Ла-Манша: BFS
проверял access, motor_vehicle и motorcar, не смотрел vehicle, выбрал в Дувре
цепочку из пяти путей с vehicle=no, добавил её в пакет и законную цепочку не
добавил вовсе. Маршрут при этом существовал — через Нормандские острова, — и
приёмка по признаку "маршрут есть" его пропустила.

Правило, которое здесь закреплено: иерархия OSM
    access -> vehicle -> motor_vehicle -> motorcar
и побеждает САМЫЙ ЧАСТНЫЙ присутствующий тег, а не самый запрещающий.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from connectors_dir import access_ok

CASES = [
    # (теги, ожидание, почему)
    ("highway=service", True,
     "обычный путь без тегов доступа"),
    ("highway=service,access=no", False,
     "access=no запрещает всё"),
    ("highway=service,access=private", False,
     "access=private запрещает всё"),
    ("highway=service,vehicle=no", False,
     "ВОТ ЭТОТ СЛУЧАЙ И СЛОМАЛ ДУВР: vehicle=no запрещает весь транспорт"),
    ("highway=service,vehicle=private", False,
     "vehicle=private — то же самое"),
    ("highway=service,vehicle=no,motor_vehicle=yes", True,
     "motor_vehicle частнее vehicle и переопределяет его"),
    ("highway=service,vehicle=no,motorcar=designated", True,
     "motorcar частнее всех и переопределяет vehicle=no"),
    ("highway=service,motor_vehicle=no,motorcar=yes", True,
     "motorcar частнее motor_vehicle"),
    ("highway=service,motorcar=no,motor_vehicle=yes", False,
     "и наоборот: motorcar=no сильнее motor_vehicle=yes"),
    ("highway=service,access=private,motor_vehicle=destination", True,
     "подъезд к терминалу часто размечен так — это разрешение"),
    ("highway=service,access=no,vehicle=yes", True,
     "vehicle частнее access"),
    ("highway=service,vehicle=destination", True,
     "destination — разрешение, а не запрет"),
    ("highway=service,vehicle=permissive", True,
     "permissive — разрешение"),
    ("highway=service,vehicle=customers", True,
     "customers — разрешение: у порта это норма"),
]

bad = 0
print("%-56s %-8s %-8s %s" % ("теги", "ждём", "вышло", "случай"))
for tags, want, why in CASES:
    got = access_ok(tags)
    ok = got == want
    if not ok:
        bad += 1
    print("%-56s %-8s %-8s %s%s" %
          (tags[:56], "да" if want else "нет", "да" if got else "нет", why,
           "" if ok else "   <== РАСХОЖДЕНИЕ"))

print("")
print("случаев %d, расхождений %d" % (len(CASES), bad))
sys.exit(1 if bad else 0)
