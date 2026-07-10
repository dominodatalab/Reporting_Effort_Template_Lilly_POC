/* cap input rows for the captured run */
options obs=100;

/* --- environment the %tfl_metadata macro expects ------------------------
   On the Domino platform these are provided by domino.sas: the global
   __prog_name (the program stem, used to pick the metadata row) and a
   METADATA library. Here we point METADATA at WORK and stand up one small
   mock metadata row so the macro has something to read. The macro logic
   itself is exercised unchanged in script.sas. */
%let __prog_name = t_pop;
libname metadata (work);

/* one metadata row: the display attributes tfl_metadata turns into macro vars */
data metadata.t_pop;
    length title1 $60 title2 $60 popn 8 pop $40;
    title1 = "Table 14.1.1";
    title2 = "Summary of Analysis Populations";
    popn   = 306;
    pop    = "Safety Analysis Set";
    output;
run;
