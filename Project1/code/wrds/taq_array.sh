#!/bin/bash
# SGE array task: one trading day per task. Submit: qsub -t 1-N -tc 8 taq_array.sh
cd "${YC_SCRATCH:?set YC_SCRATCH to your WRDS scratch folder}"
D=$(sed -n "${SGE_TASK_ID}p" day_list.txt)
[ -f taq/tr_$D.parquet ] && exit 0
sas -nodms -sysparm $D -log logs/taq_$D.log -print logs/taq_$D.lst taq_day.sas && python3 taq_to_parquet.py $D
