/*****************************************************************************\
* Program : ADSL.sas + ADVS.sas derivations  (compatibility bundle)
* Source  : prod/adam/ADSL.sas and prod/adam/ADVS.sas
*           @ dominodatalab/Reporting_Effort_Template_EL_POC
*
* These are the interactive-branch (DOMINO_IS_WORKFLOW_JOB=false) derivation
* bodies of the two ADaM programs, verbatim: ADSL is built from SDTM.DM, then
* ADVS is the BY-usubjid merge of ADAM.ADSL with SDTM.VS keeping only VS-matched
* subjects (the in=v / if v; left-join semantic). Adapted only by supplying small
* mock SDTM.DM and SDTM.VS domains (autoexec.sas) in place of the mounted SDTM
* snapshot, and by printing the results so the merge outcome is visible.
\*****************************************************************************/

/* ---- ADSL derivation, from prod/adam/ADSL.sas (interactive branch) ---- */
data adam.adsl;
   set sdtm.dm;
run;

/* ---- ADVS derivation, from prod/adam/ADVS.sas (interactive branch) ----
   merge ADSL demography onto each VS record, keeping VS-matched subjects */
data adam.advs;
	merge adam.adsl sdtm.vs (in = v);
		by usubjid;
	if v;
run;

/* show the derived ADaM datasets */
proc print data=adam.adsl noobs;
    title "ADAM.ADSL (derived from SDTM.DM)";
run;

proc print data=adam.advs noobs;
    title "ADAM.ADVS (ADSL merged onto SDTM.VS by usubjid, VS-matched only)";
run;
title;
