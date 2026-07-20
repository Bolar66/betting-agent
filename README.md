# Betting Agent

Automaattinen agentti joka analysoi tulosveto- ja maalintekijä-kertoimia 
ja ehdottaa päivittäin yhden lapun (kombinaatiovedon), jonka ihminen lyö 
manuaalisesti pienellä panoksella.

## tekijät

Ruge&Smolge

## Ympäristön pystytys

1. Cloonaa repo
2. Luo virtuaaliympäristö:
   \`\`\`
   python -m venv .venv
   .venv\Scripts\activate
   \`\`\`
3. Asenna riippuvuudet:
   \`\`\`
   pip install -r requirements.txt
   \`\`\`
4. Kopioi \`.env.example\` -> \`.env\` ja täytä oikeat API-avaimet

## Kansiorakenne

- \`data/\` — tallennettu/haettu data
- \`notebooks/\` — Jupyter-kokeilut ja mallinnus
- \`src/\` — varsinainen sovelluslogiikka
- \`tests/\` — testit