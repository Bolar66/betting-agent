# Legacy

Vanha koodi Odds API -ajalta ja kokeillut ML-mallit (voittomalli, Poisson, kalibrointi).
Ei käytössä päivittäisessä putkessa. Säilytetty mahdollista omaa mallia varten.

Ajo (osa moduuleista tarvitsee `src/`-kansion apumoduuleja):

```
pip install -r requirements.txt -r legacy/requirements.txt
set PYTHONPATH=src
python legacy/train_model.py
```
