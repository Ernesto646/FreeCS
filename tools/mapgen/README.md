# Generador de mapas para FreeCS

Traza la distribución a partir del video y guárdala en un JSON como el [ejemplo de oficina](office.example.json). El generador convierte ese plano en un `.map` Valve 220 editable, un `.wad` de texturas de prototipo y, si indicas un compilador, un `.bsp` GoldSrc con las texturas incluidas. El ejemplo es un plano ficticio para probar el flujo.

## Generar y jugar

Con Python 3.10 o posterior, ejecuta desde la raíz del repositorio:

```powershell
python tools/mapgen/generate.py tools/mapgen/office.example.json --output ./map-build
```

Para crear el `.bsp`, descarga [VHLT V34](https://github.com/twhl-community/VHLT-V34/releases/tag/V34) y pasa la carpeta que contiene sus ejecutables CSG, BSP, VIS y RAD. El generador también reconoce los ejecutables de [SDHLT](https://github.com/seedee/SDHLT/releases/tag/v1.3.1).

```powershell
python tools/mapgen/generate.py tools/mapgen/office.example.json --output ./map-build --compiler-dir 'C:/Herramientas/VHLT-V34/tools'
```

Copia `cs_oficina_demo.bsp` a `freecs/maps/` de la instalación de FreeCS. Abre la consola con **Shift + Esc** y ejecuta `map cs_oficina_demo`. Conserva juntos el `.map` y el `.wad` si quieres editarlo en J.A.C.K.

## Describir el plano

El generador construye un nivel de una planta con una envolvente rectangular cerrada. Usa el piso como `z = 0` y mide todas las coordenadas antes de aplicar `scale`.

| Campo | Contenido |
| --- | --- |
| `name` | Nombre que empieza por `cs_` para rescate o `de_` para desactivación. |
| `size` | Ancho, fondo y altura interior en unidades de mapa. |
| `scale` | Multiplicador de la arquitectura, el mobiliario y la distribución. Puedes probar otro valor con `--scale`. |
| `walls`, `props` | Cajas sólidas `[x1, y1, z1, x2, y2, z2]`. Para una puerta, divide el muro y deja el vano entre los tramos. |
| `spawns.ct`, `spawns.t` | Puntos `[x, y, ángulo]`. La altura se ajusta al tamaño estándar del jugador. FreeCS crea las zonas de compra alrededor de estos puntos. |
| `lights` | Posiciones `[x, y, z]` dentro del espacio libre. Si omites el campo, se coloca una luz al centro. |
| `objective.zones` | Cajas para las zonas de rescate o los sitios de bomba. |
| `objective.hostages` | Puntos `[x, y]` para los rehenes en mapas `cs_`. |

El ejemplo aplica `scale: 2.5`. Con esa escala, una mesa de 32 unidades pasa a medir 80 y queda por encima de la vista del personaje. Los jugadores conservan su tamaño, altura de cámara y movimiento de FreeCS. Podemos ajustar la escala del mapa real cuando veamos el video y probemos los recorridos en el juego.
