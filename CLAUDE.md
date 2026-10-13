## Vérifications (à chaque modification)

- Après toute modification de `traducteur_ecran.pyw` ou de `extension/`, lancer `py -B tests/verifier_tout.py` et ne rien livrer tant qu'il n'affiche pas « TOUT EST BON ».
- Chaque bug corrigé ajoute son cas dans `tests/verifier_tout.py`, pour qu'il ne revienne pas.
- Toute modification de `extension/` augmente `version` dans `extension/manifest.json` : l'extension, si elle n'a pas été rechargée dans le navigateur, le signale alors elle-même (« ↻ »).
- Vérifier la syntaxe avec `py -B` (jamais `py_compile`) : pas de `__pycache__` ni de fichiers de travail dans le dossier du projet.

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
