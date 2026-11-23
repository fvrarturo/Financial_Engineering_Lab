#!/bin/bash
# Premium-robust DP on the remaining 7 industries (SPEC 05 §0: deployed-strategy performance needs the DP on every
# asset the rule routes to it). Same procedure and settings as the dp_ind run; deployed-DP assets first.
set -e
cd "$(dirname "$0")/.."
for G in ind_durbl,ind_hitec,ind_manuf,ind_nodur,ind_other ind_shops,ind_utils; do
  python3 scripts/06_dp_premrobust.py solve --asset $G --jobs 6 >> reports/dp_mu/solve_ind2.log 2>&1
  for S in calibrate evaluate compare report; do
    python3 scripts/06_dp_premrobust.py $S --asset $G --jobs 6 >> reports/dp_mu/run_ind2.log 2>&1
  done
  echo "GROUP_DONE $G $(date +%H:%M:%S)" >> reports/dp_mu/run_ind2.log
done
echo PIPELINE_DONE >> reports/dp_mu/run_ind2.log
