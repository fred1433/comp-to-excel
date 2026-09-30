-- Open a workbook in Microsoft Excel for Mac, force a full recalculation, read cells back, close without saving.
-- argv: workbook path, then cell refs as "Sheet!A1". Prints "version<TAB>..." then one line per cell:
-- ref<TAB>class<TAB>value<TAB>formula
on run argv
	set inPath to item 1 of argv
	set cellRefs to items 2 thru -1 of argv
	set out to ""
	set TB to tab
	set LF to linefeed
	with timeout of 120 seconds
		tell application "Microsoft Excel"
			set appVersion to version
			set wb to open workbook workbook file name inPath
			calculate full
			repeat with cellRef in cellRefs
				set AppleScript's text item delimiters to "!"
				set sheetName to text item 1 of (cellRef as text)
				set addr to text item 2 of (cellRef as text)
				set AppleScript's text item delimiters to ""
				set c to range addr of worksheet sheetName of wb
				set v to value of c
				set f to formula of c
				set out to out & (cellRef as text) & TB & (class of v as text) & TB & (v as text) & TB & f & LF
			end repeat
			close wb saving no
		end tell
	end timeout
	return "version" & tab & appVersion & linefeed & out
end run
