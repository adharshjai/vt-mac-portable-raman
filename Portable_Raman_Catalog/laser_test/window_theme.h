#pragma once
// The look of the controller window: one set of colours for the light and one for the dark
// appearance, the style sheet built from them, and the small display-only widgets of the header
// and the left column. Header-only and without Q_OBJECT, so that every program that includes
// main.cpp (the controller, the test programs, the end-to-end test) builds it without a moc step.

#include <QApplication>
#include <QColor>
#include <QFrame>
#include <QGraphicsLayout>
#include <QIcon>
#include <QLabel>
#include <QPainter>
#include <QPalette>
#include <QStyle>
#include <QStyleFactory>
#include <QStyleHints>
#include <QVBoxLayout>
#include <QWidget>
#include <QtCharts/QChart>
#include <QtCharts/QChartView>
#include <QtCharts/QLineSeries>
#include <QtCharts/QValueAxis>

#include <algorithm>
#include <cmath>

// What a status means, independent of the colours that show it. Hazard is reserved for the laser.
enum class UiTone { Neutral, Info, Ok, Warn, Danger, Hazard };

struct UiTheme {
    bool dark = false;
    QColor window, surface, surfaceAlt, border, borderStrong, text, textDim;
    QColor accent, accentHover, accentText, accentInk, selection, series;
    QColor okInk, okFill, warnInk, warnFill, dangerInk, dangerFill, infoInk, infoFill;
    // Laser warning yellow with black, as on laser warning signs. The two differ in brightness and the
    // band adds stripes, so the warning does not rest on hue, which tinted laser goggles change. The
    // pair is the same in both appearances.
    QColor hazard = QColor("#FFC400"), hazardInk = QColor("#1A1A1A");

    static UiTheme light() {
        UiTheme t;
        t.window = "#EEF0F3"; t.surface = "#FFFFFF"; t.surfaceAlt = "#F3F5F8"; t.border = "#D4D9E1"; t.borderStrong = "#8792A1";
        t.text = "#1A1F27"; t.textDim = "#566070";
        t.accent = "#1F5FD6"; t.accentHover = "#184FB8"; t.accentText = "#FFFFFF"; t.accentInk = "#1F5FD6";
        t.selection = "#D6E4FB"; t.series = "#1F5FD6";
        t.okInk = "#156F3B"; t.okFill = "#DCF2E3"; t.warnInk = "#8A4B00"; t.warnFill = "#FFE8BF";
        t.dangerInk = "#B3261E"; t.dangerFill = "#FBDAD7"; t.infoInk = "#1552B0"; t.infoFill = "#DCE8FA";
        return t;
    }
    static UiTheme darkAppearance() {
        UiTheme t;
        t.dark = true;
        t.window = "#14171C"; t.surface = "#1D2128"; t.surfaceAlt = "#272C35"; t.border = "#363D49"; t.borderStrong = "#6B7686";
        t.text = "#E8EBEF"; t.textDim = "#A3ABB7";
        t.accent = "#2C66D3"; t.accentHover = "#3470E0"; t.accentText = "#FFFFFF"; t.accentInk = "#8DBBFF";
        t.selection = "#263F66"; t.series = "#8DBBFF";
        t.okInk = "#6DD99A"; t.okFill = "#173B28"; t.warnInk = "#FFC069"; t.warnFill = "#47330F";
        t.dangerInk = "#FF8A80"; t.dangerFill = "#4A1F1C"; t.infoInk = "#8DBBFF"; t.infoFill = "#17304F";
        return t;
    }
    // The appearance the system asks for. Where the platform does not say (the offscreen platform of
    // the test programs), the application palette decides.
    static UiTheme forApplication() {
#if QT_VERSION >= QT_VERSION_CHECK(6, 5, 0)
        const Qt::ColorScheme scheme = QGuiApplication::styleHints()->colorScheme();
        if (scheme != Qt::ColorScheme::Unknown) return scheme == Qt::ColorScheme::Dark ? darkAppearance() : light();
#endif
        return QGuiApplication::palette().color(QPalette::Window).lightness() < 128 ? darkAppearance() : light();
    }

    QColor ink(UiTone tone) const {
        switch (tone) {
        case UiTone::Info: return infoInk;
        case UiTone::Ok: return okInk;
        case UiTone::Warn: return warnInk;
        case UiTone::Danger: return dangerInk;
        case UiTone::Hazard: return hazardInk;
        case UiTone::Neutral: break;
        }
        return text;
    }
    QColor fill(UiTone tone) const {
        switch (tone) {
        case UiTone::Info: return infoFill;
        case UiTone::Ok: return okFill;
        case UiTone::Warn: return warnFill;
        case UiTone::Danger: return dangerFill;
        case UiTone::Hazard: return hazard;
        case UiTone::Neutral: break;
        }
        return surfaceAlt;
    }
    static QColor mix(const QColor& a, const QColor& b, double share) {
        return QColor::fromRgbF(a.redF() + (b.redF() - a.redF()) * share, a.greenF() + (b.greenF() - a.greenF()) * share,
                                a.blueF() + (b.blueF() - a.blueF()) * share);
    }

    // For the parts the style sheet does not reach: placeholder text, item views, disabled text.
    QPalette palette() const {
        QPalette p;
        p.setColor(QPalette::Window, window); p.setColor(QPalette::WindowText, text);
        p.setColor(QPalette::Base, surface); p.setColor(QPalette::AlternateBase, surfaceAlt);
        p.setColor(QPalette::Text, text); p.setColor(QPalette::Button, surface); p.setColor(QPalette::ButtonText, text);
        p.setColor(QPalette::ToolTipBase, surface); p.setColor(QPalette::ToolTipText, text);
        p.setColor(QPalette::PlaceholderText, mix(textDim, surface, 0.25));
        p.setColor(QPalette::Highlight, accent); p.setColor(QPalette::HighlightedText, accentText);
        p.setColor(QPalette::Link, accentInk);
        for (QPalette::ColorRole role : {QPalette::WindowText, QPalette::Text, QPalette::ButtonText})
            p.setColor(QPalette::Disabled, role, mix(textDim, surface, 0.35));
        return p;
    }

    QString styleSheet() const {
        QString css = QString::fromLatin1(R"css(
QWidget#appRoot, QDialog { background: @window; }
QWidget { color: @text; font-size: 13px; }
QToolTip { background: @surface; color: @text; border: 1px solid @borderStrong; padding: 4px 6px; }

QFrame#header { background: @surface; border: none; border-bottom: 1px solid @border; }
QLabel#appTitle { font-size: 17px; font-weight: 600; }
QLabel#appSubtitle { font-size: 11px; color: @textDim; }

QFrame#group, QFrame#card { background: @surface; border: 1px solid @border; border-radius: 8px; }
QFrame#group[alert="true"] { background: @hazardTint; border: 1px solid @hazardEdge; }
QLabel#groupTitle { font-size: 10px; font-weight: 700; color: @textDim; }
QLabel#fieldLabel, QLabel#caption { font-size: 12px; color: @textDim; }
QLabel#paneTitle { font-size: 11px; font-weight: 700; color: @textDim; }
QLabel#emptyTitle { font-size: 15px; font-weight: 600; }
QLabel#emptyText { color: @textDim; }
QLabel#demoBadge { background: @warnFill; color: @warnInk; border: 1px solid @warnInk; border-radius: 4px;
                   padding: 1px 6px; font-size: 11px; font-weight: 700; }
QLabel[tone="danger"] { color: @dangerInk; }
QLabel[tone="warn"] { color: @warnInk; }
QLabel[tone="dim"] { color: @textDim; }

QPushButton { background: @surface; border: 1px solid @borderStrong; border-radius: 6px; padding: 3px 10px; min-height: 20px; }
QPushButton:hover { background: @surfaceAlt; border-color: @textDim; }
QPushButton:pressed { background: @border; }
QPushButton:disabled { color: @disabledText; background: @surfaceAlt; border-color: @border; }
QPushButton:checked, QPushButton[engaged="true"] { background: @infoFill; border-color: @accentInk; color: @infoInk; font-weight: 600; }
QPushButton[role="primary"] { background: @accent; border-color: @accent; color: @accentText; font-weight: 600; }
QPushButton[role="primary"]:hover { background: @accentHover; border-color: @accentHover; }
QPushButton[role="primary"]:pressed { background: @accent; }
QPushButton[role="primary"]:disabled { background: @surfaceAlt; border-color: @border; color: @disabledText; }
QPushButton[role="stop"] { color: @dangerInk; border-color: @dangerInk; font-weight: 700; }
QPushButton[role="stop"]:hover { background: @dangerFill; }
QPushButton[role="stop"][urgent="true"] { background: @dangerFill; border: 2px solid @dangerInk; padding: 2px 9px; }
QPushButton[role="stop"]:disabled { color: @disabledText; background: @surfaceAlt; border-color: @border; }
QPushButton[role="quiet"] { border-color: @border; color: @textDim; }
QPushButton[role="quiet"]:hover { color: @text; }
QPushButton#laserButton { min-height: 26px; font-size: 14px; font-weight: 700; }
QPushButton#laserButton[laser="on"] { background: @hazard; color: @hazardInk; border: 2px solid @hazardInk; padding: 2px 9px; }
QPushButton#laserButton[laser="unconfirmed"] { background: @dangerFill; color: @dangerInk; border: 2px solid @dangerInk; padding: 2px 9px; }

QLineEdit, QComboBox { background: @surface; border: 1px solid @borderStrong; border-radius: 6px; padding: 2px 6px;
                       min-height: 20px; selection-background-color: @accent; selection-color: @accentText; }
QLineEdit:focus, QComboBox:focus { border: 1px solid @accentInk; }
QLineEdit:read-only { background: @surfaceAlt; color: @textDim; }
QLineEdit[missing="true"] { border: 1px solid @warnInk; }
QComboBox:disabled { background: @surfaceAlt; color: @disabledText; border-color: @border; }
QComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: center right; width: 20px; border: none; }
QComboBox::down-arrow { width: 0; height: 0; border-left: 4px solid @surface; border-right: 4px solid @surface;
                        border-top: 5px solid @textDim; margin-right: 6px; }
QComboBox::down-arrow:disabled { border-left-color: @surfaceAlt; border-right-color: @surfaceAlt; border-top-color: @disabledText; }
QComboBox QAbstractItemView { background: @surface; color: @text; border: 1px solid @borderStrong;
                              selection-background-color: @accent; selection-color: @accentText; outline: none; }

QTextEdit { background: @surface; border: 1px solid @border; border-radius: 6px;
            selection-background-color: @accent; selection-color: @accentText; }
QTextEdit#facts { background: transparent; border: none; font-size: 12px; }
QTextEdit#log { font-family: "Menlo", "Consolas", "DejaVu Sans Mono", monospace; font-size: 11px; }

QTabWidget::pane { background: @surface; border: 1px solid @border; border-radius: 8px; top: -1px; }
QTabBar { background: transparent; }
QTabBar::tab { background: transparent; border: none; border-bottom: 3px solid transparent; padding: 5px 14px 4px 14px;
               margin-right: 2px; color: @textDim; font-weight: 600; }
QTabBar::tab:hover { color: @text; }
QTabBar::tab:selected { color: @text; border-bottom: 3px solid @accentInk; }

QTableWidget, QTreeWidget { background: @surface; alternate-background-color: @surfaceAlt; border: 1px solid @border;
                            border-radius: 6px; gridline-color: @border; outline: none; font-size: 12px; }
QTableWidget::item { padding: 1px 0px; }
QTreeWidget::item { padding: 1px 2px; }
QTableWidget::item:selected, QTreeWidget::item:selected { background: @selection; color: @text; }
QHeaderView { background: @surfaceAlt; }
QHeaderView::section { background: @surfaceAlt; color: @textDim; border: none; border-bottom: 1px solid @border;
                       padding: 4px 6px; font-size: 11px; font-weight: 700; }
QTableCornerButton::section { background: @surfaceAlt; border: none; }

QScrollArea { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 0; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 0; }
QScrollBar::handle { background: @borderStrong; border-radius: 4px; min-height: 24px; min-width: 24px; margin: 1px; }
QScrollBar::handle:hover { background: @textDim; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

QSplitter::handle { background: transparent; }
QStatusBar { background: @surface; border-top: 1px solid @border; color: @textDim; }
QStatusBar::item { border: none; }
)css");
        QList<QPair<QString, QString>> colours = {
            {"@window", window.name()}, {"@surfaceAlt", surfaceAlt.name()}, {"@surface", surface.name()},
            {"@borderStrong", borderStrong.name()}, {"@border", border.name()}, {"@textDim", textDim.name()}, {"@text", text.name()},
            {"@accentHover", accentHover.name()}, {"@accentText", accentText.name()}, {"@accentInk", accentInk.name()},
            {"@accent", accent.name()}, {"@selection", selection.name()},
            {"@okInk", okInk.name()}, {"@okFill", okFill.name()}, {"@warnInk", warnInk.name()}, {"@warnFill", warnFill.name()},
            {"@dangerInk", dangerInk.name()}, {"@dangerFill", dangerFill.name()}, {"@infoInk", infoInk.name()}, {"@infoFill", infoFill.name()},
            {"@hazardInk", hazardInk.name()}, {"@hazardTint", mix(surface, hazard, dark ? 0.16 : 0.14).name()},
            {"@hazardEdge", mix(hazard, text, dark ? 0.0 : 0.35).name()}, {"@hazard", hazard.name()},
            {"@disabledText", mix(textDim, surface, 0.35).name()},
        };
        // A name that begins another one ("@text", "@textDim") must be replaced after it.
        std::sort(colours.begin(), colours.end(), [](const auto& a, const auto& b) { return a.first.size() > b.first.size(); });
        for (const auto& colour : colours) css.replace(colour.first, colour.second);
        return css;
    }
};

// The appearance in use. One per application: the system has one appearance at a time, and the
// painted widgets below read it when they draw.
inline UiTheme& uiThemeInUse() { static UiTheme theme = UiTheme::light(); return theme; }
inline const UiTheme& uiTheme() { return uiThemeInUse(); }

// A style sheet rule that depends on a property ([laser="on"]) is only re-read when the widget is polished again.
inline void setStyleProperty(QWidget* widget, const char* name, const QVariant& value) {
    if (widget->property(name) == value) return;
    widget->setProperty(name, value);
    widget->style()->unpolish(widget);
    widget->style()->polish(widget);
    widget->update();
}

inline QFont uiFont(const QWidget* widget, int pixelSize, QFont::Weight weight = QFont::Normal) {
    QFont font = widget->font();
    font.setPixelSize(pixelSize);
    font.setWeight(weight);
    return font;
}

// One status of the header: a caption that says which part ("LASER") and its state in words.
// The state is always written out; the colour and the dot only repeat it.
class StatusPill : public QWidget {
public:
    explicit StatusPill(const QString& caption, QWidget* parent = nullptr) : QWidget(parent), captionText(caption) {
        setSizePolicy(QSizePolicy::Fixed, QSizePolicy::Fixed);
    }
    // A pill that may give way when the header is too narrow for everything: its text is then cut with
    // an ellipsis, and stays whole in the tool tip. The laser, scan and backend pills never give way.
    void setShrinkable() {
        shrinkable = true;
        setSizePolicy(QSizePolicy::Preferred, QSizePolicy::Fixed);
    }
    // prominent: the larger, heavier form, for the states that must not be overlooked.
    void setState(UiTone tone, const QString& value, const QString& tip = QString(), bool prominent = false) {
        const QString fullTip = shrinkable ? (captionText + " " + value).trimmed() + (tip.isEmpty() ? QString() : "\n" + tip) : tip;
        if (toolTip() != fullTip) setToolTip(fullTip);
        if (tone == currentTone && value == valueText && prominent == isProminent) return;
        currentTone = tone; valueText = value; isProminent = prominent;
        setAccessibleName(captionText + " " + valueText);
        updateGeometry();
        update();
    }
    QString value() const { return valueText; }
    UiTone tone() const { return currentTone; }
    bool prominent() const { return isProminent; }
    QSize sizeHint() const override {
        return QSize(fixedPart() + QFontMetrics(valueFont()).horizontalAdvance(valueText), isProminent ? 32 : 26);
    }
    QSize minimumSizeHint() const override {
        const QSize whole = sizeHint();
        return shrinkable ? QSize(std::min(whole.width(), fixedPart() + 64), whole.height()) : whole;
    }

protected:
    void paintEvent(QPaintEvent*) override {
        const UiTheme& theme = uiTheme();
        const QColor ink = theme.ink(currentTone), fill = theme.fill(currentTone);
        QPainter painter(this);
        painter.setRenderHint(QPainter::Antialiasing);
        const qreal edge = isProminent ? 2 : 1;
        const QRectF box = QRectF(rect()).adjusted(edge / 2, edge / 2, -edge / 2, -edge / 2);
        painter.setPen(QPen(isProminent ? ink : UiTheme::mix(fill, ink, 0.45), edge));
        painter.setBrush(fill);
        painter.drawRoundedRect(box, box.height() / 2, box.height() / 2);
        int x = kPad;
        painter.setPen(Qt::NoPen);
        painter.setBrush(currentTone == UiTone::Neutral ? theme.textDim : ink);
        painter.drawEllipse(QPointF(x + kDot / 2.0, height() / 2.0), kDot / 2.0, kDot / 2.0);
        x += kDot + kGap;
        if (!captionText.isEmpty()) {
            painter.setFont(captionFont());
            painter.setPen(currentTone == UiTone::Neutral ? theme.textDim : UiTheme::mix(ink, fill, 0.2));
            painter.drawText(QRect(x, 0, width() - x, height()), Qt::AlignVCenter | Qt::AlignLeft, captionText);
            x += painter.fontMetrics().horizontalAdvance(captionText) + kGap;
        }
        painter.setFont(valueFont());
        painter.setPen(ink);
        painter.drawText(QRect(x, 0, width() - x, height()), Qt::AlignVCenter | Qt::AlignLeft,
                         painter.fontMetrics().elidedText(valueText, Qt::ElideRight, width() - x - kPad + 1));
    }

private:
    static constexpr int kPad = 11, kDot = 8, kGap = 6;
    QFont captionFont() const { return uiFont(this, isProminent ? 11 : 10, QFont::Bold); }
    QFont valueFont() const { return uiFont(this, isProminent ? 14 : 12, isProminent ? QFont::Bold : QFont::DemiBold); }
    // Everything but the value: the padding, the dot and the caption.
    int fixedPart() const {
        const int caption = captionText.isEmpty() ? 0 : QFontMetrics(captionFont()).horizontalAdvance(captionText) + kGap;
        return kPad + kDot + kGap + caption + kPad;
    }
    QString captionText, valueText;
    UiTone currentTone = UiTone::Neutral;
    bool isProminent = false, shrinkable = false;
};

// The strip under the header. Idle it is empty; while the laser is on, or its state is not known,
// it shows warning stripes across the whole window: a pattern, not only a colour.
class HazardBand : public QWidget {
public:
    explicit HazardBand(QWidget* parent = nullptr) : QWidget(parent) { setFixedHeight(8); }
    void setActive(bool on) { if (on != active) { active = on; update(); } }
    bool isActive() const { return active; }

protected:
    void paintEvent(QPaintEvent*) override {
        if (!active) return;
        const UiTheme& theme = uiTheme();
        QPainter painter(this);
        painter.fillRect(rect(), theme.hazard);
        painter.setRenderHint(QPainter::Antialiasing);
        painter.setPen(Qt::NoPen);
        painter.setBrush(theme.hazardInk);
        const int h = height();
        for (int x = -h; x < width(); x += 24)
            painter.drawPolygon(QPolygonF({QPointF(x, h), QPointF(x + h, 0), QPointF(x + h + 12, 0), QPointF(x + 12, h)}));
    }

private:
    bool active = false;
};

// The line over the chart: what happened last, when, and whether it was good or bad news.
class EventLine : public QWidget {
public:
    explicit EventLine(QWidget* parent = nullptr) : QWidget(parent) {
        setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Fixed);
    }
    void setEvent(UiTone tone, const QString& time, const QString& text) {
        currentTone = tone; timeText = time; messageText = text;
        setToolTip(text);
        setAccessibleName("Last event: " + text);
        update();
    }
    QString text() const { return messageText; }
    UiTone tone() const { return currentTone; }
    QSize sizeHint() const override { return QSize(240, 36); }
    QSize minimumSizeHint() const override { return QSize(120, 36); }

protected:
    void paintEvent(QPaintEvent*) override {
        const UiTheme& theme = uiTheme();
        QPainter painter(this);
        painter.setRenderHint(QPainter::Antialiasing);
        painter.setPen(Qt::NoPen);
        painter.setBrush(theme.fill(currentTone));
        painter.drawRoundedRect(rect(), 6, 6);
        painter.setBrush(currentTone == UiTone::Neutral ? theme.borderStrong : currentTone == UiTone::Hazard ? theme.hazardInk
                                                                                                             : theme.ink(currentTone));
        painter.drawRoundedRect(QRectF(0, 0, 5, height()), 2.5, 2.5);
        int x = 14;
        const QColor ink = theme.ink(currentTone);
        if (!timeText.isEmpty()) {
            painter.setFont(uiFont(this, 12, QFont::DemiBold));
            painter.setPen(currentTone == UiTone::Neutral ? theme.textDim : UiTheme::mix(ink, theme.fill(currentTone), 0.2));
            painter.drawText(QRect(x, 0, width() - x, height()), Qt::AlignVCenter | Qt::AlignLeft, timeText);
            x += painter.fontMetrics().horizontalAdvance(timeText) + 10;
        }
        painter.setFont(uiFont(this, 15, QFont::DemiBold));
        painter.setPen(ink);
        const QString shown = painter.fontMetrics().elidedText(messageText, Qt::ElideRight, std::max(0, width() - x - 10));
        painter.drawText(QRect(x, 0, width() - x - 10, height()), Qt::AlignVCenter | Qt::AlignLeft, shown);
    }

private:
    UiTone currentTone = UiTone::Neutral;
    QString timeText, messageText;
};

// One line of status text that gives way when space is short: it is cut with an ellipsis and the
// full text stays in the tool tip. A QLabel would force its whole width on the window instead.
class ElidedLabel : public QWidget {
public:
    // cut: where the text is shortened; a path keeps its end, a sentence its beginning.
    explicit ElidedLabel(Qt::TextElideMode cut = Qt::ElideRight, QWidget* parent = nullptr) : QWidget(parent), cut(cut) {
        setSizePolicy(QSizePolicy::Preferred, QSizePolicy::Fixed);
    }
    void setText(const QString& text, UiTone tone = UiTone::Neutral) {
        if (text == shownText && tone == currentTone) return;
        shownText = text; currentTone = tone;
        setToolTip(text);
        setAccessibleName(text);
        updateGeometry();
        update();
    }
    QString text() const { return shownText; }
    UiTone tone() const { return currentTone; }
    QSize sizeHint() const override {
        const QFontMetrics metrics(textFont());
        return QSize(metrics.horizontalAdvance(shownText) + 2, metrics.height() + 4);
    }
    QSize minimumSizeHint() const override { return QSize(40, sizeHint().height()); }

protected:
    void paintEvent(QPaintEvent*) override {
        QPainter painter(this);
        painter.setFont(textFont());
        painter.setPen(currentTone == UiTone::Neutral ? uiTheme().textDim : uiTheme().ink(currentTone));
        painter.drawText(rect(), Qt::AlignVCenter | Qt::AlignLeft, painter.fontMetrics().elidedText(shownText, cut, width()));
    }

private:
    QFont textFont() const { return uiFont(this, 12, currentTone == UiTone::Neutral ? QFont::Normal : QFont::DemiBold); }
    const Qt::TextElideMode cut;
    QString shownText;
    UiTone currentTone = UiTone::Neutral;
};

// A titled box of the left column.
class TitledGroup : public QFrame {
public:
    explicit TitledGroup(const QString& title, QWidget* parent = nullptr) : QFrame(parent) {
        setObjectName("group");
        box = new QVBoxLayout(this);
        box->setContentsMargins(10, 7, 10, 9);
        box->setSpacing(6);
        auto* caption = new QLabel(title.toUpper());
        caption->setObjectName("groupTitle");
        box->addWidget(caption);
    }
    QVBoxLayout* body() const { return box; }
    void setAlert(bool on) { setStyleProperty(this, "alert", on); }

private:
    QVBoxLayout* box = nullptr;
};

// The mark of a capture made with the simulators, for a table cell. It is an icon because the end
// of a cell's text is cut off when the column is narrow, and this mark must never be the part that goes.
inline QIcon simulatedTag() {
    const UiTheme& theme = uiTheme();
    const qreal ratio = 2;      // drawn at twice the size, so that it is sharp on a high-resolution screen
    QPixmap pixmap(QSize(30, 16) * ratio);
    pixmap.setDevicePixelRatio(ratio);
    pixmap.fill(Qt::transparent);
    QPainter painter(&pixmap);
    painter.setRenderHint(QPainter::Antialiasing);
    painter.setPen(theme.infoInk);
    painter.setBrush(theme.infoFill);
    painter.drawRoundedRect(QRectF(0.5, 0.5, 29, 15), 3, 3);
    QFont font = QApplication::font();
    font.setPixelSize(9); font.setBold(true);
    painter.setFont(font);
    painter.drawText(QRectF(0, 0, 30, 16), Qt::AlignCenter, "SIM");
    painter.end();
    QIcon icon(pixmap);
    icon.addPixmap(pixmap, QIcon::Selected);    // the same in a selected row, not tinted by the style
    return icon;
}

// The style sheet is written on top of one base style, so that the window looks the same on every
// platform and in the offscreen snapshots of the test programs.
inline void useUiBaseStyle() { QApplication::setStyle(QStyleFactory::create("Fusion")); }

// Tick marks at round values (0, 500, 1000 ...) instead of the range cut into equal parts: as many
// as the axis has room for, chosen again when the range or the size of the chart changes.
inline void useRoundTicks(QChart* chart, QValueAxis* axis, Qt::Orientation direction) {
    const auto choose = [chart, axis, direction] {
        const qreal span = axis->max() - axis->min();
        if (!(span > 0)) return;
        // A label with the gap to the next takes about 80 pixels along a horizontal axis and 30 along a
        // vertical one. Before the chart has been laid out its size is not known; six marks then.
        const qreal length = direction == Qt::Horizontal ? chart->plotArea().width() : chart->plotArea().height();
        const qreal room = length > 0 ? std::max(1.0, length / (direction == Qt::Horizontal ? 80.0 : 30.0)) : 6.0;
        const qreal power = std::pow(10.0, std::floor(std::log10(span / room)));
        const qreal steps[] = {0.5 * power, power, 2 * power, 2.5 * power, 5 * power, 10 * power};
        int chosen = 1;
        while (chosen < 5 && span / steps[chosen] > room) ++chosen;
        if (span / steps[chosen] < 3) --chosen;      // rather a little tight than only one or two marks
        // Labels without decimals ("%.0f") would repeat, or round, for marks that are not whole numbers apart:
        // a flat spectrum spans a few counts only.
        const bool whole = axis->labelFormat().contains(".0f");
        axis->setTickInterval(whole ? std::max<qreal>(1, std::floor(steps[chosen])) : steps[chosen]);
    };
    axis->setTickType(QValueAxis::TicksDynamic);
    axis->setTickAnchor(0);
    QObject::connect(axis, &QValueAxis::rangeChanged, axis, choose);
    QObject::connect(chart, &QChart::plotAreaChanged, axis, choose);
    choose();
}

inline void applyChartTheme(const UiTheme& theme, QChartView* view, QValueAxis* x, QValueAxis* y, QLineSeries* series) {
    QChart* chart = view->chart();
    // The chart fills its view: by default it keeps a margin, which shows as a frame in the view's own colour.
    view->setBackgroundBrush(theme.surface);
    chart->layout()->setContentsMargins(0, 0, 0, 0);
    chart->setBackgroundBrush(theme.surface);
    chart->setBackgroundPen(Qt::NoPen);
    chart->setBackgroundRoundness(0);
    chart->setTitleBrush(theme.text);
    QFont title = chart->titleFont();
    title.setPixelSize(13); title.setWeight(QFont::DemiBold);
    chart->setTitleFont(title);
    for (QValueAxis* axis : {x, y}) {
        QFont labels = axis->labelsFont();
        labels.setPixelSize(11);
        axis->setLabelsFont(labels);
        QFont name = axis->titleFont();
        name.setPixelSize(12); name.setWeight(QFont::DemiBold);
        axis->setTitleFont(name);
        axis->setLabelsColor(theme.textDim);
        axis->setTitleBrush(theme.textDim);
        axis->setLinePenColor(theme.borderStrong);
        axis->setGridLineColor(theme.border);
    }
    QPen line(theme.series);
    line.setWidthF(1.6);
    series->setPen(line);
}
