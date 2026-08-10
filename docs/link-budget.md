# Link budget

The arithmetic behind `config.yml: rf`, in one place. Every value here is
pre-registered; this document derives the numbers, it does not tune them.

## From EIRP to RSRP

RSRP is defined per resource element, so the transmit power spreads across
the subcarriers of the carrier before any propagation loss applies:

```
per-subcarrier EIRP = 60 dBm - 10*log10(600) = 60 - 27.78 = 32.22 dBm
```

60 dBm EIRP is a conventional rural macro figure; 600 subcarriers is 10 MHz
LTE (50 resource blocks x 12).

## The loss the budget can afford

```
32.22 dBm  per-subcarrier EIRP
- (-105 dBm)  coverage threshold (FCC BDC 4G LTE reference value)
= 137.2 dB  gross budget (the "137 dB" quoted in config.yml)
- 8 dB     lognormal shadow-fading margin (~90% cell-edge at sigma = 8 dB)
= 129.2 dB  maximum tolerable MEDIAN path loss
```

The kernel subtracts the margin from every link, so the number the median
path loss is judged against is 129.2 dB; the gross 137.2 dB is what a single
lucky link could tolerate.

## What distance that buys

Free-space loss at 700 MHz:

| Distance | FSPL |
|---|---|
| 10 km | 109.3 dB |
| 20 km | 115.3 dB |
| 30 km | 118.9 dB |
| 40 km | 121.4 dB |

So even at 40 km the free-space budget leaves ~8 dB for diffraction and
clutter — which is why `max_link_km: 40` is a generous pair-generation cap
rather than a binding constraint.

## The constraint that actually binds

The radio horizon. With the effective-earth factor k = 4/3:

```
d_horizon = sqrt(2 k R h)      R = 6371 km
50 m mast  -> ~29 km
1.5 m handset -> ~5 km
total ~34 km
```

Links past ~34 km from a 50 m mast are obstructed by the curvature of the
earth itself, regardless of budget. The kernel models this through the
effective-earth sagitta in the diffraction geometry, and
`test_earth_curvature_blocks_beyond_the_radio_horizon` asserts the
line-of-sight flag flips at the computed horizon. The 40 km cap deliberately
sits beyond the typical horizon so that tall structures — WV registers masts
over 300 m, with horizons past 70 km — are not clipped a priori.

## Clutter

Receiver-side excess loss, the max of two descriptions of the same street:

- WorldCover class constants (`config.yml: clutter`): 12 dB tree cover,
  15 dB built-up, deliberately coarse round numbers. Fitting them to the FCC
  data used for validation would make the validation circular.
- Building knife edge (`config.yml: buildings`): the pixel's tallest Overture
  building at a 15 m setback through ITU-R P.526 J(v), capped at 30 dB
  (COST-231's rooftop-to-street term tops out near 25-30 dB; past that a
  single ray is claiming precision multipath does not allow).

Consistency between the two, measured not assumed: the median WV building
(3.55 m) evaluates to 14.8 dB against the 15 dB class constant.

## What is deliberately not in the budget

- Antenna gain patterns and downtilt (isotropic EIRP).
- Interference margin: this is a coverage model, not a capacity model.
- Building penetration loss: outdoor coverage at 1.5 m.
- Body loss, feeder loss: absorbed into the conventional EIRP figure.
