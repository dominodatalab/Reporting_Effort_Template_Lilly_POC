/* cap input rows for the captured run */
options obs=100;

/* --- stand in for the mounted SDTM/ADaM libraries ------------------------
   The interactive branch of the ADaM programs reads from an SDTM library
   (DM, VS) mounted on the Domino platform and writes to ADAM. Here we point
   both at WORK and supply small mock SDTM.DM and SDTM.VS domains with the
   columns the derivations read (usubjid plus the VS measurements). */
libname sdtm (work);
libname adam (work);

/* mock SDTM.DM: one row per subject (demography source for ADSL) */
data sdtm.dm;
    length usubjid $12 arm $30 sex $1 country $3;
    infile datalines dlm=',' dsd truncover;
    input usubjid $ arm $ age sex $ country $;
    datalines;
SUBJ-0001,Placebo,54,F,USA
SUBJ-0002,Placebo,61,M,USA
SUBJ-0003,Xanomeline Low Dose,68,M,GBR
SUBJ-0004,Xanomeline Low Dose,73,F,GBR
SUBJ-0005,Xanomeline High Dose,66,F,DEU
SUBJ-0006,Xanomeline High Dose,84,M,DEU
;
run;

/* mock SDTM.VS: several vitals records per subject (BY usubjid) */
data sdtm.vs;
    length usubjid $12 vstestcd $8 visit $12;
    infile datalines dlm=',' dsd truncover;
    input usubjid $ vstestcd $ visit $ vsstresn;
    datalines;
SUBJ-0001,SYSBP,BASELINE,128
SUBJ-0001,SYSBP,WEEK 4,124
SUBJ-0001,DIABP,BASELINE,82
SUBJ-0002,SYSBP,BASELINE,135
SUBJ-0002,DIABP,BASELINE,88
SUBJ-0003,SYSBP,BASELINE,141
SUBJ-0003,SYSBP,WEEK 4,138
SUBJ-0004,SYSBP,BASELINE,119
SUBJ-0005,SYSBP,BASELINE,150
SUBJ-0005,DIABP,BASELINE,95
SUBJ-0006,SYSBP,BASELINE,133
;
run;
