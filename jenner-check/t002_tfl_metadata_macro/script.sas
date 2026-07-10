/*****************************************************************************\
* Program : tfl_metadata.sas caller  (compatibility bundle)
* Source  : share/macros/tfl_metadata.sas @ dominodatalab/Reporting_Effort_Template_EL_POC
*
* The %tfl_metadata macro below is the author's, verbatim: it copies the
* per-program metadata row into WORK.metadata and then walks the _numeric_ and
* _character_ arrays with call symput/vname to expose every metadata column as
* a like-named macro variable. This bundle defines it and calls it (the mock
* metadata row is stood up in autoexec.sas), then prints the resulting macro
* variables to show the resolution works end to end.
\*****************************************************************************/

/* ---- author's macro, verbatim from share/macros/tfl_metadata.sas ---- */
%macro tfl_metadata();
	data metadata;
		set metadata.&__prog_name.;
	run;

	** create macro variables for all variable names;
	data _null_;
		set metadata;

		* numeric variables;
		array xxx{*} _numeric_;
		do i =1 to dim(xxx);
			call symput(vname(xxx[i]),xxx[i]);
		end;

		* character variables;
		array yyy{*} $ _character_;
		do i =1 to dim(yyy);
			call symput(vname(yyy[i]),yyy[i]);
		end;
	run;
%mend;

/* ---- caller: run the macro, then show the macro variables it created ---- */
%tfl_metadata();

%put NOTE: tfl_metadata resolved title1 = %superq(title1);
%put NOTE: tfl_metadata resolved title2 = %superq(title2);
%put NOTE: tfl_metadata resolved pop    = %superq(pop);
%put NOTE: tfl_metadata resolved popn   = %superq(popn);

/* use the derived macro variables the way a TFL program would */
title1 "&title1";
title2 "&title2";

proc print data=metadata noobs label;
    label title1 = "Title 1" title2 = "Title 2" pop = "Population" popn = "N";
run;
title;
