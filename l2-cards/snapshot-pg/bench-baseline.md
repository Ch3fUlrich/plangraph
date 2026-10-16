| backend | items | write p50 ms | write p95 ms | GET snapshot p50 ms | snapshot bytes |
|---|---|---|---|---|---|
| sqlite | 100 | 10 | 21 | 6 | 130412 |
| sqlite | 400 | 22 | 27 | 19 | 518776 |
| sqlite | 1000 | 59 | 68 | 54 | 1296579 |
| sqlite | 2000 | 108 | 134 | 91 | 2597919 |
| backend | items | write p50 ms | write p95 ms | GET snapshot p50 ms | snapshot bytes |
|---|---|---|---|---|---|
| postgres | 100 | 95 | 111 | 83 | 130412 |
| postgres | 400 | 353 | 442 | 332 | 518776 |
| postgres | 1000 | 871 | 933 | 870 | 1296579 |
| postgres | 2000 | 1710 | 1848 | 1661 | 2597919 |
