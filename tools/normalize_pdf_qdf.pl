#!/usr/bin/env perl
use strict;
use warnings;

my ($infile, $outfile) = @ARGV;
die "usage: $0 INFILE OUTFILE\n" unless defined $infile && defined $outfile;

open my $in, '<', $infile or die "open($infile): $!";
local $/;
my $text = <$in>;
close $in;

my %font_prefix;
my $next_prefix = 0;

sub stable_prefix {
	my ($n) = @_;
	my @chars = ('A' .. 'Z');
	my $prefix = '';

	for (1 .. 6) {
		$prefix = $chars[$n % 26] . $prefix;
		$n = int($n / 26);
	}

	return $prefix;
}

$text =~ s{
	([A-Z]{6})\+
	([A-Za-z0-9\-]+?)
	(?:-(Identity-[HV]|UTF16))?
	\b
}{
	my $full = $2;
	my $suffix = defined($3) ? "-$3" : "";
	my $core = $full;
	$core =~ s/-(?:Identity-[HV]|UTF16)$//;

	$font_prefix{$core} //= stable_prefix($next_prefix++);
	$font_prefix{$core} . '+' . $core . $suffix;
}gex;

$text =~ s{/ID \[<[^>]+><[^>]+>\]}{/ID [<00000000000000000000000000000000><00000000000000000000000000000000>]}g;

open my $out, '>', $outfile or die "open($outfile): $!";
print {$out} $text;
close $out;
