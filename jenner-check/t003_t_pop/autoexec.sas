/* cap input rows for the captured run */
options obs=100;

/* --- stand in for the Domino platform environment ------------------------
   The interactive branch of t_pop.sas reads adam.adsl and calls %tfl_metadata,
   which reads metadata.&__prog_name.. On the Domino platform both libraries are
   mounted; here we point ADAM and METADATA at WORK and supply small mock inputs
   with the exact columns t_pop reads (usubjid, actarm, age, sex for ADSL; the
   display/footnote attributes for the metadata row). */
%let __prog_name = t_pop;
libname adam (work);
libname metadata (work);

/* mock ADSL: 30 subjects spread across all three arms, both sexes, ages that
   land in every age band t_pop derives (trtan/agen/sexn are derived by t_pop). */
data adam.adsl;
    length usubjid $12 actarm $30 sex $1;
    infile datalines dlm=',' dsd truncover;
    input usubjid $ actarm $ age sex $;
    datalines;
SUBJ-0001,Placebo,54,F
SUBJ-0002,Placebo,61,M
SUBJ-0003,Placebo,67,F
SUBJ-0004,Placebo,72,M
SUBJ-0005,Placebo,77,F
SUBJ-0006,Placebo,83,M
SUBJ-0007,Placebo,59,F
SUBJ-0008,Placebo,66,M
SUBJ-0009,Placebo,71,F
SUBJ-0010,Placebo,88,M
SUBJ-0011,Xanomeline Low Dose,55,M
SUBJ-0012,Xanomeline Low Dose,62,F
SUBJ-0013,Xanomeline Low Dose,68,M
SUBJ-0014,Xanomeline Low Dose,73,F
SUBJ-0015,Xanomeline Low Dose,79,M
SUBJ-0016,Xanomeline Low Dose,81,F
SUBJ-0017,Xanomeline Low Dose,58,M
SUBJ-0018,Xanomeline Low Dose,64,F
SUBJ-0019,Xanomeline Low Dose,69,M
SUBJ-0020,Xanomeline Low Dose,85,F
SUBJ-0021,Xanomeline High Dose,57,F
SUBJ-0022,Xanomeline High Dose,63,M
SUBJ-0023,Xanomeline High Dose,66,F
SUBJ-0024,Xanomeline High Dose,74,M
SUBJ-0025,Xanomeline High Dose,78,F
SUBJ-0026,Xanomeline High Dose,84,M
SUBJ-0027,Xanomeline High Dose,56,F
SUBJ-0028,Xanomeline High Dose,65,M
SUBJ-0029,Xanomeline High Dose,70,F
SUBJ-0030,Xanomeline High Dose,90,M
;
run;

/* mock metadata row: t_pop's %tfl_metadata turns these columns into the
   &DisplayName / &DisplayTitle / &Title1 / &Footer1-3 macro vars the report uses */
data metadata.t_pop;
    length DisplayName $40 DisplayTitle $60 Title1 $60
           Footer1 $80 Footer2 $80 Footer3 $80;
    DisplayName  = "Table 14.1.1";
    DisplayTitle = "Summary of Analysis Populations";
    Title1       = "Safety Analysis Set";
    Footer1      = "n = number of subjects in age group.";
    Footer2      = "Percentages are based on total subjects per treatment.";
    Footer3      = "Program: t_pop.sas";
    output;
run;
