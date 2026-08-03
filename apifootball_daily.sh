#!/bin/bash
cd /home/noc/oraculo_v2
ARG_PENDING=$(python3 -c "
import os, json
d='.oraculo_cache/apifootball_hist/arg_cards'
sd=os.path.join(d,'stats')
ids=set()
for s in (2022,2023,2024):
    fp=os.path.join(d, 'fixtures_128_%d.json'%s)
    if os.path.exists(fp):
        for fx in json.load(open(fp)):
            ids.add(fx['fixture']['id'])
pending=[i for i in ids if not os.path.exists(os.path.join(sd,'%d.json'%i))]
print(len(pending))
" 2>/dev/null)
if [ "$ARG_PENDING" != "0" ] && [ -n "$ARG_PENDING" ]; then
    python3 _apifootball_historical_fetch.py --league 128 --seasons 2022,2023,2024 --out arg_cards --budget 75
else
    python3 _apifootball_historical_fetch.py --league 13 --seasons 2022,2023,2024 --out lib_btts --budget 75
fi
