| backend | items | write p50 ms | write p95 ms | write max ms | GET snapshot p50 ms | snapshot bytes |
|---|---|---|---|---|---|---|
| sqlite | 100 | 4 | 5 | 15 | 6 | 130412 |
| sqlite | 400 | 5 | 11 | 15 | 14 | 518776 |
| sqlite | 1000 | 4 | 6 | 12 | 48 | 1296579 |
| sqlite | 2000 | 5 | 6 | 6 | 63 | 2597919 |
| backend | items | write p50 ms | write p95 ms | write max ms | GET snapshot p50 ms | snapshot bytes |
|---|---|---|---|---|---|---|
| postgres | 100 | 8 | 16 | 28 | 9 | 130412 |
| postgres | 400 | 8 | 10 | 11 | 18 | 518776 |
| postgres | 1000 | 8 | 11 | 14 | 39 | 1296579 |
| postgres | 2000 | 10 | 16 | 16 | 83 | 2597919 |
done
