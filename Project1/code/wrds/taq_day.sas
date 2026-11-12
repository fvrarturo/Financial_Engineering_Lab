/* Extract one day of TAQ trades + NBBO (sampled to last quote per second) for Treasury ETFs.
   Usage: sas -nodms -sysparm YYYYMMDD taq_day.sas  -> $YC_SCRATCH/taq/{tr,nb}_YYYYMMDD.csv */
%let base=%sysget(YC_SCRATCH);
%let d=&sysparm;
libname ct "/wrds/nyse/sasdata/taqms/ct";
libname nb "/wrds/nyse/sasdata/wrds_taqms_nbbo";
%let syms="SGOV","BIL","SHV","TBIL","XBIL","XHLF","XONE","UTWO","UTRE","UFIV","USVN","UTEN","UTWY","UTHY",
 "SHY","IEI","IEF","TLH","TLT","GOVT","VGSH","VGIT","VGLT","SCHO","SCHR","SCHQ","SPTS","SPTI","SPTL","EDV","ZROZ","GOVZ";
data tr; set ct.ctm_&d(keep=sym_root sym_suffix time_m price size ex tr_corr tr_scond
     where=(sym_root in (&syms) and sym_suffix="" and tr_corr="00"));
  drop sym_suffix tr_corr; run;
data nbq; set nb.complete_nbbo_&d(keep=sym_root sym_suffix time_m best_bid best_ask best_bidsizeshares best_asksizeshares
     where=(sym_root in (&syms) and sym_suffix="" and best_bid>0 and best_ask>best_bid));
  sec=floor(time_m); drop sym_suffix; run;
proc sort data=nbq; by sym_root sec time_m; run;
data nb1; set nbq; by sym_root sec; if last.sec; drop time_m; run;
proc export data=tr outfile="&base/taq/tr_&d..csv" dbms=csv replace; run;
proc export data=nb1 outfile="&base/taq/nb_&d..csv" dbms=csv replace; run;
proc sql; select sym_root, count(*) as n from tr group by sym_root; select count(*) as nbbo_raw from nbq; select count(*) as nbbo_1s from nb1; quit;
