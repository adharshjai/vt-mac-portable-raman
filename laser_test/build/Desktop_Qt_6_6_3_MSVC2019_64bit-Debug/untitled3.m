% HW1 Q1(b) - OPL scan + Snell Verification
clear; clc; close all;

% numbers from problem 1
a = 1; b = 2; c = 3; n1 = 1; n2 = 1.5;

% OPL from problem 1
AC = @(x) sqrt(a^2 + (c - x).^2);
BC = @(x) sqrt(b^2 + x.^2);
OPL = @(x) n1*AC(x) + n2*BC(x);

% Scan & minium
x = linspace(0, c, 100);
opl = OPL(x);
[xs, opls] = fminbnd(OPL, 0, c);

% Degrees
ACs = AC(xs); BCs = BC(xs);
sa = (c - xs)/ACs;              % sin(theta_a)
sb = (xs)/BCs;                  % sin(theta_b)
ta = asin(sa); tb = asin(sb);   % Radian

% Snell check
lhs = n1*sa; rhs = n2*sb;
fprintf('x* = %.6f m\nOPL(x*) = %.10f\n', xs, opls);
fprintf('theta_a = %.4f deg, theta_b = %.4f deg\n', rad2deg(ta), rad2deg(tb));
fprintf('n1*sin(theta_a) = %.8f, n2*sin(theta_b) = %.8f, diff = %.2e\n', lhs, rhs, abs(lhs-rhs));

% plot
plot(x, opl, 'LineWidth', 1.4); hold on;
plot(xs, opls, 'ro', 'MarkerSize', 6, 'LineWidth', 1.2);
xlabel('x = B''C (m)'); ylabel('OPL(x)');
title('OPL vs x'); grid on; legend('OPL', 'min');
