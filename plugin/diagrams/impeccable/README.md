# impeccable command map

Edit `data.py` for command copy (`ONE`, `HOW`, `GET`, `NEXT` feed the click popover), `gen.py` for the grouped left-to-right layout, and `build.py` for the page wrapper. The main rail reads setup → plan/build → diagnose → optional fix/style/motion groups → polish; utilities have a separate bottom lane.

The diagram is a hand-built inline SVG, so a rebuild needs only Python 3 (no Archify):

```bash
python3 gen.py              # writes diagram.svg
python3 build.py index.html # inlines it with the click popover
```

Commit `diagram.svg` and the finished `index.html` with the sources.
