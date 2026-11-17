#pragma once
// The Data and Review tabs: what the data layer knows about the saved spectra, and the actions on
// it (data_layer/PROTOCOL.md section 6). Everything goes through DataLayerClient, so nothing here
// waits for the data layer, and no dialog blocks: the acquisition code shares this event loop.
// Header-only and without Q_OBJECT (see window_theme.h).

#include "data_layer_client.h"
#include "window_status.h"
#include "window_theme.h"

#include <QComboBox>
#include <QDateTime>
#include <QDialog>
#include <QFileDialog>
#include <QGridLayout>
#include <QHBoxLayout>
#include <QHeaderView>
#include <QJsonArray>
#include <QLineEdit>
#include <QPointer>
#include <QPushButton>
#include <QScrollArea>
#include <QSplitter>
#include <QStackedLayout>
#include <QStackedWidget>
#include <QTableWidget>
#include <QTextEdit>
#include <QTreeWidget>

#include <cmath>
#include <limits>

// How the values of section 6 are worded in the tables.
namespace DataText {
inline QString none() { return QStringLiteral("\u2014"); }
inline QString localTime(const QString& utc, const QString& format) {
    const QDateTime time = QDateTime::fromString(utc, Qt::ISODateWithMs);
    return time.isValid() ? time.toLocalTime().toString(format) : utc;
}
inline QString score(double value) { return QString::number(value, 'f', 2); }
// A score below the threshold it was judged by. It is never rounded up, so that it cannot read as the
// threshold itself (0.797, not 0.80, next to a threshold of 0.80), also where the threshold is not at
// hand: list items do not carry it. A third place where two would round up; past that it is cut.
inline QString lowScore(double value) {
    for (int digits = 2; digits <= 3; ++digits) {
        const QString text = QString::number(value, 'f', digits);
        if (text.toDouble() <= value) return text;
    }
    return QString::number(std::floor(value * 1000) / 1000, 'f', 3);
}
inline QString score(double value, double threshold) { return value < threshold ? lowScore(value) : score(value); }
// The model's top score, worded by its outcome: a low-confidence one is below the threshold.
inline QString confidence(const QJsonObject& p) {
    const double value = p["confidence"].toDouble();
    return p["outcome"].toString() == "low_confidence" ? lowScore(value) : score(value);
}
inline QString sample(const QJsonObject& item) {
    const QString id = item["sample_id"].toString();
    return id.isEmpty() ? none() : id;
}
inline QString point(const QJsonObject& item) {
    if (item["scan_mode"].toString() == "single") return "single";
    return item["point_id"].isDouble() ? QString::number(item["point_id"].toInt()) : none();
}
inline QString topScore(const QJsonObject& p) {
    const QString top = p["top_class"].toString();
    return top.isEmpty() ? QString() : top + " " + confidence(p);
}
// The model's own label as it stands in the record: "r6g 0.97", or "UNKNOWN (r6g 0.61)" when the label
// is not the top class. For the details pane and the tool tips.
inline QString modelLabel(const QJsonValue& value) {
    if (!value.isObject()) return none();
    const QJsonObject p = value.toObject();
    const QString label = p["label"].toString(), top = p["top_class"].toString(), scored = topScore(p);
    if (label.isEmpty() || label == top) return scored.isEmpty() ? none() : scored;
    return scored.isEmpty() ? label : label + " (" + scored + ")";
}
// What the model concluded, worded by its outcome (data_layer/PROTOCOL.md section 5.3). The label alone
// does not tell them apart: a background spectrum and an uncertain one are both UNKNOWN.
inline QString prediction(const QJsonValue& value) {
    if (!value.isObject()) return none();
    const QJsonObject p = value.toObject();
    const QString outcome = p["outcome"].toString(), scored = topScore(p);
    if (outcome == "rejected") return "unusable";
    if (scored.isEmpty()) return modelLabel(p);
    if (outcome == "background") return scored + " (background)";
    if (outcome == "low_confidence") return p["top_class"].toString() + "? " + confidence(p) + " (uncertain)";
    return outcome == "confident" ? scored : modelLabel(p);
}
// A label a person settled on, in the words of the tables: REJECTED and UNKNOWN as review-set stores them.
inline QString reviewLabel(const QString& label) {
    return label == "REJECTED" ? QString("unusable") : label == "UNKNOWN" ? QString("unknown") : label;
}
inline bool decided(const QString& status) { return status == "confirmed" || status == "relabelled" || status == "rejected"; }
// The Prediction cell of the Data table: once a person has decided, the decision leads and the model's
// own answer follows in brackets.
inline QString predictionCell(const QJsonObject& item) {
    const QJsonObject p = item["prediction"].toObject(), r = item["review"].toObject();
    const QString status = r["status"].toString();
    if (p.isEmpty() || !decided(status)) return prediction(item["prediction"]);
    const QString model = p["outcome"].toString() == "rejected" ? QString("unusable") : topScore(p);
    const QString label = status == "rejected" ? QString("unusable") : reviewLabel(r["label"].toString());
    // A confirmed prediction is the model's own answer, said once.
    if (status == "confirmed" && (label == model || label == p["top_class"].toString())) return (model.isEmpty() ? label : model) + ", confirmed";
    const QString settled = label.isEmpty() ? status : label + ", " + (status == "rejected" ? QString("by review") : status);
    return model.isEmpty() ? settled : settled + " (model: " + model + ")";
}
inline QString review(const QJsonValue& value) {
    if (!value.isObject()) return none();
    const QJsonObject r = value.toObject();
    const QString status = r["status"].toString(), label = reviewLabel(r["label"].toString());
    // Nothing is said where no review is needed: the column then shows only what waits or was decided.
    if (status == "not_required") return none();
    QString text = status == "pending" ? QString("to review") : status == "rejected" ? QString("marked unusable")
                 : decided(status) && !label.isEmpty() ? status + ": " + label : status;
    // Recorded on this Mac while the catalog could not be reached.
    if (r["queued"].toBool()) text += " (not sent yet)";
    return text;
}
inline UiTone stateTone(const QString& state) {
    if (state == "error") return UiTone::Danger;
    if (state == "needs_review") return UiTone::Warn;
    if (state == "accepted" || state == "reviewed") return UiTone::Ok;
    return state == "captured" ? UiTone::Neutral : UiTone::Info;
}
}

// A table of rows to pick from. Its narrow columns take the width of their content and the others
// share the rest in given proportions (a header view on its own can only share it equally).
class ShareTable : public QTableWidget {
public:
    // shares: 0 for a column as wide as its content, else its part of the remaining width.
    ShareTable(const QStringList& headers, const QList<int>& shares, QWidget* parent = nullptr)
        : QTableWidget(0, int(headers.size()), parent), shares(shares) {
        setHorizontalHeaderLabels(headers);
        verticalHeader()->hide();
        verticalHeader()->setMinimumSectionSize(20);
        verticalHeader()->setDefaultSectionSize(23);
        setSelectionBehavior(QAbstractItemView::SelectRows);
        setSelectionMode(QAbstractItemView::SingleSelection);
        setEditTriggers(QAbstractItemView::NoEditTriggers);
        setAlternatingRowColors(true);
        setShowGrid(false);
        setWordWrap(false);
        setIconSize(QSize(30, 16));
        setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
        horizontalHeader()->setHighlightSections(false);
        horizontalHeader()->setDefaultAlignment(Qt::AlignLeft | Qt::AlignVCenter);
        horizontalHeader()->setMinimumSectionSize(36);
        horizontalHeader()->setSectionResizeMode(QHeaderView::Fixed);
    }
    // Called after the rows have changed, and whenever the table's width does.
    void fitColumns() {
        int taken = 0, parts = 0, lastShared = -1;
        for (int column = 0; column < shares.size(); ++column) {
            if (shares[column] > 0) { parts += shares[column]; lastShared = column; continue; }
            resizeColumnToContents(column);
            taken += columnWidth(column);
        }
        int left = std::max(0, viewport()->width() - taken);
        const int available = left;
        for (int column = 0; column < shares.size(); ++column) {
            if (shares[column] == 0) continue;
            const int width = column == lastShared ? left : available * shares[column] / parts;
            setColumnWidth(column, width);
            left -= width;
        }
    }

protected:
    // The width that counts is the viewport's: it also changes when the vertical scroll bar comes or goes.
    bool viewportEvent(QEvent* event) override {
        const bool handled = QTableWidget::viewportEvent(event);
        if (event->type() == QEvent::Resize) fitColumns();
        return handled;
    }

private:
    const QList<int> shares;
};

// The table with the details pane beside it. Until the divider is moved by hand, the pane gets a
// width that suits the window: narrow beside a narrow table, so that the table's columns stay readable.
class DetailsSplitter : public QSplitter {
public:
    DetailsSplitter() : QSplitter(Qt::Horizontal) {
        setChildrenCollapsible(false);
        connect(this, &QSplitter::splitterMoved, this, [this] { movedByHand = true; });
    }

protected:
    void resizeEvent(QResizeEvent* event) override {
        QSplitter::resizeEvent(event);
        if (movedByHand || count() != 2) return;
        const int pane = std::clamp(width() * 2 / 5 - 130, 210, 340);
        setSizes({width() - pane, pane});
    }

private:
    bool movedByHand = false;
};

// Facts about one capture: rich text that is as tall as its content, like a label, but that breaks
// a long identifier wherever it must and lets the text be selected and copied.
class FactsText : public QTextEdit {
public:
    explicit FactsText(QWidget* parent = nullptr) : QTextEdit(parent) {
        setObjectName("facts");
        setReadOnly(true);
        setFrameShape(QFrame::NoFrame);
        setWordWrapMode(QTextOption::WrapAtWordBoundaryOrAnywhere);
        setVerticalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
        setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
        setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Fixed);
        document()->setDocumentMargin(0);
    }
    void setFacts(const QString& html) {
        setHtml(html);
        fit();
    }

protected:
    void resizeEvent(QResizeEvent* event) override {
        QTextEdit::resizeEvent(event);
        fit();
    }

private:
    void fit() {
        document()->setTextWidth(viewport()->width());
        setFixedHeight(int(std::ceil(document()->size().height())) + 2);
    }
};

// The provenance chain of section 6 ("nodes", "edges") as a small tree: every record that is derived
// from others stands on a line of its own, with the records it is derived from indented below it.
class ProvenanceTree : public QTreeWidget {
public:
    explicit ProvenanceTree(QWidget* parent = nullptr) : QTreeWidget(parent) {
        setColumnCount(3);
        setHeaderHidden(true);
        setRootIsDecorated(false);
        setItemsExpandable(false);
        setSelectionMode(QAbstractItemView::NoSelection);
        setFocusPolicy(Qt::NoFocus);
        setIndentation(16);
        setUniformRowHeights(true);
        setTextElideMode(Qt::ElideRight);
        setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
        header()->setStretchLastSection(true);
        header()->setSectionResizeMode(0, QHeaderView::ResizeToContents);
        header()->setSectionResizeMode(1, QHeaderView::ResizeToContents);
        setToolTip("Each record is derived from the records indented below it.");
    }

    void setGraph(const QJsonObject& graph) {
        clear();
        QHash<QString, QJsonObject> nodes;
        QStringList order;
        for (const QJsonValue& value : graph["nodes"].toArray()) {
            const QJsonObject node = value.toObject();
            nodes.insert(node["id"].toString(), node);
            order << node["id"].toString();
        }
        QHash<QString, QStringList> sources;    // record -> the records it is derived from
        QSet<QString> used;
        for (const QJsonValue& value : graph["edges"].toArray()) {
            const QJsonObject edge = value.toObject();
            sources[edge["from"].toString()] << edge["to"].toString();
            used.insert(edge["to"].toString());
        }
        for (const QString& id : order) {
            if (used.contains(id) && !sources.contains(id)) continue;    // shown below what is derived from it
            QTreeWidgetItem* item = row(nodes.value(id));
            addTopLevelItem(item);
            for (const QString& source : sources.value(id))
                if (nodes.contains(source)) item->addChild(row(nodes.value(source)));
        }
        expandAll();
    }
    int rowCount() const {
        int rows = 0;
        for (int i = 0; i < topLevelItemCount(); ++i) rows += 1 + topLevelItem(i)->childCount();
        return rows;
    }
    bool hasDerivedRecord() const {
        for (int i = 0; i < topLevelItemCount(); ++i)
            if (topLevelItem(i)->childCount() > 0) return true;
        return false;
    }
    // Tall enough for this many lines; more than that scrolls.
    int heightForRows(int rows) const {
        const int line = topLevelItemCount() > 0 ? sizeHintForRow(0) : fontMetrics().height() + 4;
        return rows * line + 2 * frameWidth() + 4;
    }

    static QString kind(const QJsonObject& node) {
        const QString type = node["type"].toString(), alias = node["alias"].toString();
        if (type == "raman.raw_spectrum" || alias.startsWith("rs-")) return "Spectrum";
        if (type == "raman.prediction" || alias.startsWith("rp-")) return "Prediction";
        if (alias.startsWith("rmodel-") || type.contains("model")) return "Model";
        if (alias.startsWith("rdark-") || type.contains("dark")) return "Dark";
        return type.isEmpty() ? QString("Record") : type;
    }
    static QString what(const QJsonObject& node) {
        const QJsonObject info = node["info"].toObject();
        const QString name = kind(node);
        if (name == "Spectrum" && info.contains("sample_id"))
            return info["point_id"].isDouble() ? uiJoin({info["sample_id"].toString(), QString("point %1").arg(info["point_id"].toInt())})
                                               : info["sample_id"].toString();
        if (name == "Prediction" && (info.contains("label") || info.contains("top_class"))) return DataText::prediction(info);
        if (name == "Model" && info.contains("name")) return info["name"].toString();
        // The column is narrow and the kind is already said: of dark_20261113_101400_001.txt the time stamp is what tells.
        if (name == "Dark" && info.contains("file"))
            return QFileInfo(info["file"].toString()).completeBaseName().remove(QRegularExpression("^dark_"));
        return node["title"].toString();
    }

protected:
    // An empty box would look broken; like the score bars, it says that there is nothing to show.
    void paintEvent(QPaintEvent* event) override {
        QTreeWidget::paintEvent(event);
        if (topLevelItemCount() > 0) return;
        QPainter painter(viewport());
        painter.setFont(uiFont(this, 12));
        painter.setPen(uiTheme().textDim);
        painter.drawText(viewport()->rect().adjusted(6, 0, -6, 0), Qt::AlignVCenter | Qt::AlignLeft, "No provenance to show.");
    }
    // The lines that tie a record to the records it is derived from; the base style draws none.
    void drawBranches(QPainter* painter, const QRect& rect, const QModelIndex& index) const override {
        if (!index.parent().isValid()) return;
        const bool last = !index.sibling(index.row() + 1, 0).isValid();
        const int x = rect.right() - indentation() / 2, middle = rect.center().y();
        painter->save();
        painter->setPen(uiTheme().borderStrong);
        painter->drawLine(x, rect.top(), x, last ? middle : rect.bottom());
        painter->drawLine(x, middle, rect.right() - 2, middle);
        painter->restore();
    }

private:
    QTreeWidgetItem* row(const QJsonObject& node) {
        auto* item = new QTreeWidgetItem({kind(node), node["id"].toString(), what(node)});
        const QString tip = QStringList{node["title"].toString(), node["info"].toObject()["file"].toString(), node["alias"].toString()}
                                .join("\n").simplified();
        for (int column = 0; column < 3; ++column) item->setToolTip(column, tip);
        item->setForeground(0, uiTheme().textDim);
        // The record the selection is about, wherever it stands in the chain.
        if (node["role"].toString() == "start") {
            QFont bold = font();
            bold.setBold(true);
            for (int column = 0; column < 3; ++column) item->setFont(column, bold);
            item->setForeground(0, uiTheme().accentInk);
        }
        return item;
    }
};

// The class scores of one prediction as bars, highest first, with the review threshold marked.
class ScoreBars : public QWidget {
public:
    explicit ScoreBars(QWidget* parent = nullptr) : QWidget(parent) {
        setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Fixed);
    }
    void setScores(const QJsonObject& scores, double threshold) {
        rows.clear();
        for (auto it = scores.begin(); it != scores.end(); ++it) rows.append({it.key(), it.value().toDouble()});
        std::sort(rows.begin(), rows.end(), [](const auto& a, const auto& b) { return a.second != b.second ? a.second > b.second : a.first < b.first; });
        limit = threshold;
        updateGeometry();
        update();
    }
    QStringList classes() const {
        QStringList names;
        for (const auto& entry : rows) names << entry.first;
        return names;
    }
    QSize sizeHint() const override { return QSize(200, std::max(1, int(rows.size())) * kRow + 2); }
    QSize minimumSizeHint() const override { return QSize(120, sizeHint().height()); }

protected:
    void paintEvent(QPaintEvent*) override {
        const UiTheme& theme = uiTheme();
        QPainter painter(this);
        painter.setRenderHint(QPainter::Antialiasing);
        if (rows.isEmpty()) {
            painter.setFont(uiFont(this, 12));
            painter.setPen(theme.textDim);
            painter.drawText(rect(), Qt::AlignVCenter | Qt::AlignLeft, "No scores to show.");
            return;
        }
        // The top class is drawn heavier; the name column is measured in that weight so that no name is cut.
        const QFontMetrics heavy(uiFont(this, 12, QFont::DemiBold));
        int nameWidth = 0;
        for (const auto& entry : rows) nameWidth = std::max(nameWidth, heavy.horizontalAdvance(entry.first) + 2);
        nameWidth = std::min(nameWidth, width() / 2);
        // The top score is the one the threshold decides on; it is written so that it reads that way.
        const auto valueText = [this](int i) { return i == 0 && limit > 0 ? DataText::score(rows[i].second, limit) : DataText::score(rows[i].second); };
        const int valueWidth = std::max(heavy.horizontalAdvance("0.00"), heavy.horizontalAdvance(valueText(0))) + 4;
        const QRectF track(nameWidth + 8, 0, std::max(10, width() - nameWidth - valueWidth - 16), height());
        for (int i = 0; i < rows.size(); ++i) {
            const qreal top = i * kRow;
            const double value = std::clamp(rows[i].second, 0.0, 1.0);
            const bool first = i == 0;
            painter.setPen(first ? theme.text : theme.textDim);
            painter.setFont(uiFont(this, 12, first ? QFont::DemiBold : QFont::Normal));
            painter.drawText(QRectF(0, top, nameWidth, kRow), Qt::AlignVCenter | Qt::AlignLeft,
                             painter.fontMetrics().elidedText(rows[i].first, Qt::ElideRight, nameWidth));
            painter.drawText(QRectF(width() - valueWidth, top, valueWidth, kRow), Qt::AlignVCenter | Qt::AlignRight, valueText(i));
            const QRectF bar(track.left(), top + 5, track.width(), kRow - 10);
            painter.setPen(Qt::NoPen);
            painter.setBrush(theme.surfaceAlt);
            painter.drawRoundedRect(bar, 3, 3);
            // The top class is what the decision rests on: green at or above the threshold, amber below.
            painter.setBrush(!first ? theme.borderStrong : value >= limit ? theme.okInk : theme.warnInk);
            painter.drawRoundedRect(QRectF(bar.left(), bar.top(), bar.width() * value, bar.height()), 3, 3);
        }
        if (limit > 0 && limit < 1) {
            const qreal x = track.left() + track.width() * limit;
            painter.setPen(QPen(theme.text, 1, Qt::DashLine));
            painter.drawLine(QPointF(x, 1), QPointF(x, rows.size() * kRow - 1));
        }
    }

private:
    static constexpr int kRow = 22;
    QList<QPair<QString, double>> rows;
    double limit = 0;
};

// Uploads of finished sessions to ARC, the lower part of the Data tab behind its ARC upload button.
// It shows its uploads with the table, the facts box and the words above.
#include "arc_panel.h"

// The two tabs and what drives them: the status, the lists, the actions. One object for both,
// because they show the same captures and every action in one changes what the other shows.
class DataPanels : public QObject {
    friend struct HandshakeTest;
    friend struct ArcUiTest;

public:
    // operatorField: the Operator field of the window; its text is the reviewer of a review.
    DataPanels(const QString& home, const QString& fallbackInterpreter, QLineEdit* operatorField, QObject* parent)
        : QObject(parent), dataLayer(new DataLayerClient(home, fallbackInterpreter, this)),
          studio(new StudioLauncher(dataLayer, this)), reviewerField(operatorField) {
        if (!dataLayer->installed()) {
            dataTab = notInstalledPage();
            reviewTab = notInstalledPage();
            return;
        }
        dataTab = buildDataPage();
        reviewTab = buildReviewPage();
        autoSync.setInterval(30000);
        connect(&autoSync, &QTimer::timeout, this, [this] {
            // Without a status the data layer did not answer last time: ask again. Else sync while
            // there is something a sync can move on.
            if (!statusProblem.isEmpty()) refresh();
            else if (periodicSyncDue()) syncWhenIdle();
        });
        connect(reviewerField, &QLineEdit::textChanged, this, [this] { updateControls(); });
        applyTheme();
        updateControls();
    }

    QWidget* dataPage() const { return dataTab; }
    QWidget* reviewPage() const { return reviewTab; }
    bool installed() const { return dataLayer->installed(); }
    bool started() const { return isStarted; }
    int reviewsWaiting() const { return int(reviewItems.size()); }
    // Called when the header should read pillState() and reviewsWaiting() again.
    std::function<void()> changed;

    // The first status and list. Until then nothing has been started.
    void start() {
        if (!installed() || isStarted) return;
        isStarted = true;
        refresh();
        autoSync.start();
        updateControls();
        arcPageShown();
    }
    // The window says whether an acquisition is under way: a scan, a capture, a laser command. A sync
    // that starts by itself waits until none is, so that no process is started in the middle of one.
    void setAcquisitionBusy(bool busy) {
        acquisitionBusy = busy;
        if (!busy && syncDue) syncWhenIdle();
        if (arcPanel) arcPanel->setAcquisitionBusy(busy);
    }
    // A scan or a single capture has ended: its points are registered now, not at the next interval.
    // Its session is offered for ARC as well.
    void captureFinished() {
        syncWhenIdle();
        if (arcPanel) arcPanel->captureFinished();
    }
    // One of the two tabs has come to the front. Another program may have changed the catalog since
    // (the Simulation Studio, a classifier job): read the rows again, unless that has just been done.
    void pageShown() {
        if (isStarted && (!lastRefresh.isValid() || lastRefresh.elapsed() > 3000)) refresh();
        arcPageShown();
    }

    void openStudio(const StudioLauncher::Done& done) { studio->open(done); }

    // The "DATA" status of the header: which catalog, whether it is reachable, what a sync has still to
    // move into it, and what no sync moves by itself ("stuck"). The count says what it is: beside
    // "Review (N)", a bare "0 waiting" read as if nothing needed anyone. What waits for a review is that
    // tab's count, and the words are short, so that the pill does not push the SIMULATION badge aside
    // at the smallest window size.
    PillState pillState() const {
        if (!installed()) return {UiTone::Neutral, "not installed", "This copy has no data layer; spectra are saved in scan_sessions only."};
        if (!statusProblem.isEmpty()) return {UiTone::Danger, "unavailable", statusProblem, true};
        if (status.isEmpty()) return {UiTone::Neutral, "checking...", "Asking the data layer for its status."};
        const bool offline = catalogOffline();
        // The counts are of the catalog used before; none of them says what this one holds.
        const int toSync = catalogChanged() ? 0 : syncWork(), stuck = catalogChanged() ? 0 : needsAttention();
        QStringList parts{"demo"};
        if (offline) parts << "offline";
        if (catalogChanged()) parts << "not synced yet";
        if (toSync > 0) parts << QString("%1 to sync").arg(toSync);
        if (stuck > 0) parts << QString("%1 stuck").arg(stuck);
        if (toSync == 0 && stuck == 0 && !offline && !catalogChanged()) parts << "synced";
        QString tip = catalogText() + "\n" + summaryText();
        if (stuck > 0)
            tip += QString("\n%1 stuck: no sync moves %2 on by %3. The Data tab says why: Import scan folder for a capture "
                           "without its fields, Sync now for what failed too often.")
                       .arg(stuck).arg(stuck == 1 ? "it" : "them", stuck == 1 ? "itself" : "themselves");
        const UiTone tone = offline || stuck > 0 ? UiTone::Warn : toSync > 0 || catalogChanged() ? UiTone::Info : UiTone::Ok;
        return {tone, uiJoin(parts), tip};
    }

    // Colours that were put on items, icons and rich text are fixed when those are made: make them again.
    void applyTheme() {
        if (!installed()) return;
        applyChartTheme(uiTheme(), reviewChartView, reviewAxisX, reviewAxisY, reviewSeries);
        showCaptures();
        captureProvenance->setGraph(captureGraph);
        showReviewList();
        reviewProvenance->setGraph(reviewGraph);
        arcPanel->applyTheme();
        colourArcToggle();
    }

protected:
    // The ARC page comes into view or leaves it: its toggle, the Data tab, the window.
    bool eventFilter(QObject* watched, QEvent* event) override {
        if (arcPanel && watched == arcPanel->page() && (event->type() == QEvent::Show || event->type() == QEvent::Hide)) arcPageShown();
        return QObject::eventFilter(watched, event);
    }

private:
    static constexpr int kListLimit = 500;
    static inline const QString kArcToggleTip = "Show the uploads of finished sessions to ARC. Press again to show the captures.";

    // ------------------------------------------------------------------ what the status says

    // What is on this Mac only: captures not yet uploaded, and reviews recorded while offline.
    int waiting() const { return status["pending"].toInt() + status["reviews_queued"].toInt(); }
    // Uploaded captures that have no prediction yet: Classify now has to run.
    int toClassify() const {
        const QJsonObject counts = status["counts"].toObject();
        return counts["uploaded"].toInt() + counts["classified"].toInt();
    }
    // What a plain sync can move on now, as the data layer counts it ("sync_work"): captures to register
    // or upload, failed ones it still retries, reviews recorded offline. Not what waits for a person: a
    // capture without its fields (import-session), one that failed too often (Sync now). A data layer
    // that does not report it gets every capture and review that waits here counted.
    int syncWork() const { return status["sync_work"].isDouble() ? status["sync_work"].toInt() : waiting(); }
    // What waits on this Mac for a person rather than for a sync: captures without their fields (Import
    // scan folder) or past sync.max_attempts, and reviews past it (Sync now). A dark spectrum still on
    // its way counts in sync_work alone, so while one is, this can come out lower; never higher.
    int needsAttention() const { return std::max(0, waiting() - syncWork()); }
    // The rows still describe the catalog used before the settings changed ("catalog_changed"): their
    // states, record ids and predictions are that catalog's, and the next sync registers every capture
    // in this one. A data layer that does not report it never says so.
    bool catalogChanged() const { return status["catalog_changed"].toBool(); }
    // Whether the interval of the periodic sync has something for it. Predictions are made by Classify
    // now and only a sync picks them up.
    bool periodicSyncDue() const {
        // Until a sync has moved the rows to the catalog in use, what they count says nothing about it.
        return catalogChanged() || syncWork() > 0 || toClassify() > 0;
    }
    bool catalogOffline() const { return status["online"].isBool() && !status["online"].toBool(); }
    QString catalogText() const {
        QString text = status["mode_label"].toString();
        // The demo is called a demo here even if the data layer's own label should ever not say so.
        if (!text.contains("demo", Qt::CaseInsensitive)) text = "Local demo catalog: " + text;
        QStringList parts{text};
        if (catalogOffline()) parts << "switched offline for the demo";
        return uiJoin(parts);
    }
    QString summaryText() const {
        const QJsonObject counts = status["counts"].toObject();
        int total = 0;
        for (auto it = counts.begin(); it != counts.end(); ++it) total += it.value().toInt();
        QStringList parts{QString("%1 capture%2").arg(total).arg(total == 1 ? "" : "s")};
        if (catalogChanged()) {
            parts << QString("the next sync registers them in the demo catalog");
        } else {
            // "pending" also counts the rows in error, which are counted once, as a problem.
            const int notUploaded = std::max(0, status["pending"].toInt() - counts["error"].toInt());
            if (notUploaded > 0) parts << QString("%1 not uploaded yet").arg(notUploaded);
            const int unsent = status["reviews_queued"].toInt();
            if (unsent > 0) parts << QString("%1 review%2 kept on this Mac").arg(unsent).arg(unsent == 1 ? "" : "s");
            if (toClassify() > 0) parts << QString("%1 to classify").arg(toClassify());
            if (counts["needs_review"].toInt() > 0) parts << QString("%1 to review").arg(counts["needs_review"].toInt());
            if (counts["error"].toInt() > 0) parts << QString("%1 with a problem").arg(counts["error"].toInt());
        }
        const QString last = status["last_sync_utc"].toString();
        parts << (last.isEmpty() ? QString("no sync yet") : "last sync " + DataText::localTime(last, "HH:mm:ss"));
        if (captureTotal > captures.size()) parts << QString("the newest %1 are listed").arg(captures.size());
        return uiJoin(parts);
    }
    // "Demo sync at 10:15:02 (nothing leaves this Mac)": the words in front of every result, so that a
    // demo action never reads as if data had been sent.
    QString actionPrefix(const QString& action) const {
        const QString time = QTime::currentTime().toString("HH:mm:ss");
        return QString("Demo %1 at %2 (nothing leaves this Mac)").arg(action.toLower(), time);
    }
    static QString failureText(const QString& what, const DataLayerReply& reply) {
        return what + ": " + reply.error + (reply.detail.isEmpty() ? QString() : " (" + reply.detail.simplified() + ")");
    }
    static QString problemsText(const QJsonArray& errors) {
        if (errors.isEmpty()) return QString();
        const QJsonValue first = errors.first();
        const QString message = first.isObject() ? first.toObject()["message"].toString() : first.toString();
        return QString(" %1 problem%2: %3").arg(errors.size()).arg(errors.size() == 1 ? "" : "s").arg(message);
    }

    void setStatus(const QJsonObject& newStatus) {
        status = newStatus;
        statusProblem.clear();
        arcPanel->statusUpdated(status);
        showStatus();
    }
    void showStatus() {
        catalogBadge->show();
        catalogLabel->setText(catalogText());
        setStyleProperty(catalogLabel, "tone", QVariant());
        summaryLabel->setText(summaryText());
        QStringList problems;
        for (const QJsonValue& problem : status["problems"].toArray()) problems << problem.toString();
        problemsLabel->setVisible(!problems.isEmpty());
        problemsLabel->setText(problems.isEmpty() ? QString()
            : problems.first() + (problems.size() > 1 ? QString(" (+%1 more)").arg(problems.size() - 1) : QString()), UiTone::Warn);
        problemsLabel->setToolTip(problems.join("\n"));
        // The states are those of the demo catalog on this Mac: "Uploaded" must not read as sent to a service.
        // Nor may the states of the catalog used before the settings changed read as this one's.
        QTableWidgetItem* stateHeader = captureTable->horizontalHeaderItem(3);
        stateHeader->setText(catalogChanged() ? "State (previous catalog)" : "State (demo)");
        stateHeader->setToolTip(catalogChanged() ? "The states in the catalog used before the settings changed. The next sync registers "
                                                   "every capture in the demo catalog."
                                : "The states in the local demo catalog on this Mac; nothing is sent anywhere.");
        updateControls();
        if (changed) changed();
    }
    void showStatusProblem(const DataLayerReply& reply) {
        statusProblem = failureText("The data layer did not give its status", reply);
        catalogBadge->hide();
        catalogLabel->setText(statusProblem);
        setStyleProperty(catalogLabel, "tone", "danger");
        summaryLabel->setText(QString());
        problemsLabel->hide();
        updateControls();
        if (changed) changed();
    }

    // ------------------------------------------------------------------ commands

    // Status, then the rows of both tabs. A request made while one is running is done once more afterwards.
    void refresh(bool withStatus = true) {
        if (!isStarted) return;
        if (refreshRunning) { refreshAgain = true; return; }
        refreshRunning = true;
        if (!withStatus) { loadLists(); return; }
        dataLayer->run({"status"}, [this](const DataLayerReply& reply) {
            if (reply.ok()) setStatus(reply.json);
            else showStatusProblem(reply);
            // Without any answer the interpreter itself is the problem; two more attempts would only repeat it.
            if (reply.ok() || !reply.json.isEmpty()) loadLists();
            else refreshDone();
        });
    }
    void loadLists() {
        dataLayer->run({"list", "--limit", QString::number(kListLimit)}, [this](const DataLayerReply& reply) {
            if (reply.ok()) {
                captures = reply.json["items"].toArray();
                captureTotal = reply.json["total"].toInt(int(captures.size()));
                showCaptures();
                noteSentReviews();
                if (!status.isEmpty()) summaryLabel->setText(summaryText());
            } else {
                activityLabel->setText(failureText("The captures could not be listed", reply), UiTone::Danger);
            }
            dataLayer->run({"review-list"}, [this](const DataLayerReply& reply) {
                if (reply.ok()) {
                    reviewItems = reply.json["items"].toArray();
                    showReviewList();
                } else {
                    reviewResult->setText(failureText("The review list could not be read", reply), UiTone::Danger);
                }
                refreshDone();
            });
        });
    }
    void refreshDone() {
        refreshRunning = false;
        lastRefresh.start();
        if (changed) changed();
        if (refreshAgain) { refreshAgain = false; refresh(); }
    }

    void syncWhenIdle() {
        syncDue = acquisitionBusy;
        if (!acquisitionBusy) sync(false);
    }
    // retryFailed: also try again what failed so often that the data layer stopped retrying it. That is
    // what pressing Sync now asks for; a sync that starts by itself leaves those captures alone.
    // quiet: the sync completes another action, whose result stays in view; only a failure replaces it.
    void sync(bool retryFailed, bool quiet = false) {
        if (!isStarted) return;
        // What was saved while a sync was already on its way is picked up by one more.
        if (syncRunning) { syncAgain = true; return; }
        syncRunning = true;
        if (!quiet) activityLabel->setText("Demo sync running...");
        updateControls();
        dataLayer->run(retryFailed ? QStringList{"sync", "--retry-failed"} : QStringList{"sync"}, [this, quiet](const DataLayerReply& reply) {
            syncRunning = false;
            if (reply.ok()) {
                if (reply.json["status"].isObject()) setStatus(reply.json["status"].toObject());
                if (!quiet) showSyncResult(reply.json);
                refresh(!reply.json["status"].isObject());
            } else {
                activityLabel->setText(failureText("Sync failed", reply), UiTone::Danger);
            }
            updateControls();
            if (syncAgain) { syncAgain = false; sync(false); }
        });
    }
    // Captures that cannot be registered show as "Error" rows and in the problems line; here they are
    // only counted, so that a sync that otherwise went well does not read as a failure.
    void showSyncResult(const QJsonObject& result) {
        if (result["busy"].toBool()) { activityLabel->setText("Another sync is already running; its result will show here."); return; }
        const QString prefix = actionPrefix("Sync") + ": ";
        const QString problems = problemsText(result["errors"].toArray());
        if (result["online"].isBool() && !result["online"].toBool()) {
            activityLabel->setText(prefix + "the demo catalog is switched offline"
                                   + QString("; %1 capture(s) wait on this Mac.").arg(waiting()) + problems, UiTone::Warn);
            return;
        }
        QStringList parts;
        const auto count = [&](const char* key, const QString& one, const QString& many) {
            const int n = result[key].toInt();
            if (n > 0) parts << QString("%1 %2").arg(n).arg(n == 1 ? one : many);
        };
        count("ingested", "new capture found", "new captures found");
        count("registered", "registered", "registered");
        count("uploads_started", "upload started", "uploads started");
        count("uploads_done", "upload finished", "uploads finished");
        count("predictions_found", "prediction picked up", "predictions picked up");
        count("reviews_sent", "review sent", "reviews sent");
        activityLabel->setText(prefix + (parts.isEmpty() ? QString("nothing new") : parts.join(", ")) + "." + problems);
    }

    void classify() {
        if (!isStarted || classifyRunning) return;
        classifyRunning = true;
        activityLabel->setText("Classifier running on this Mac...");
        updateControls();
        dataLayer->run({"classify"}, [this](const DataLayerReply& reply) {
            classifyRunning = false;
            updateControls();
            if (!reply.ok()) { activityLabel->setText(failureText("Classify failed", reply), UiTone::Danger); return; }
            const QJsonValue made = reply.json["predictions"];
            const int predictions = made.isArray() ? int(made.toArray().size()) : made.toInt();
            const QString problems = problemsText(reply.json["errors"].toArray());
            const int later = reply.json["skipped"].toInt() + reply.json["remaining"].toInt();
            activityLabel->setText(QString("%1: %2 processed, %3 prediction(s) made%4 (runner: %5).%6")
                                       .arg(actionPrefix("Classify")).arg(reply.json["processed"].toInt()).arg(predictions)
                                       .arg(later > 0 ? QString(", %1 left for later").arg(later) : QString())
                                       .arg(reply.json["runner"].toString("local"), problems),
                                   problems.isEmpty() ? UiTone::Neutral : UiTone::Warn);
            // classify has put its predictions into the rows already, except where a row still waits for
            // its upload to be confirmed: the sync that confirms it brings those. Its own result would
            // read "nothing new" in place of this one, so it is not shown.
            sync(false, true);
        });
    }

    // Local demo only: pretend the catalog cannot be reached, to show captures waiting and catching up.
    void setOffline(bool offline) {
        if (!isStarted || offlineRunning) return;
        offlineRunning = true;
        updateControls();
        dataLayer->run({"offline", offline ? "--on" : "--off"}, [this](const DataLayerReply& reply) {
            offlineRunning = false;
            if (reply.ok())
                activityLabel->setText(reply.json["offline"].toBool() ? "The demo catalog is switched offline: new captures wait on this Mac."
                                                                       : "The demo catalog is reachable again: the next sync registers what waited.");
            else
                activityLabel->setText(failureText("The demo catalog could not be switched", reply), UiTone::Danger);
            updateControls();
            refresh();
        });
    }

    // Free text may begin with a dash; "--option=text" keeps the data layer from reading it as an option.
    static QString option(const QString& name, const QString& text) { return name + "=" + text; }

    void importFolder(const QString& folder, const QString& sample, const QString& operatorName, const QString& location) {
        if (!isStarted || importRunning || folder.trimmed().isEmpty()) return;
        QStringList command{"import-session", option("--folder", folder.trimmed())};
        if (!sample.trimmed().isEmpty()) command << option("--sample-id", sample.trimmed());
        if (!operatorName.trimmed().isEmpty()) command << option("--operator", operatorName.trimmed());
        if (!location.trimmed().isEmpty()) command << option("--location", location.trimmed());
        importRunning = true;
        activityLabel->setText("Importing " + QFileInfo(folder.trimmed()).fileName() + "...");
        updateControls();
        dataLayer->run(command, [this, folder](const DataLayerReply& reply) {
            importRunning = false;
            updateControls();
            if (!reply.ok()) { activityLabel->setText(failureText("Import failed", reply), UiTone::Danger); return; }
            const QString problems = problemsText(reply.json["errors"].toArray());
            activityLabel->setText(QString("Imported %1%2: %3 capture(s) added to the list, %4 skipped.%5")
                                       .arg(reply.json["folder"].toString(QFileInfo(folder.trimmed()).fileName()),
                                            reply.json["copied"].toBool() ? " (copied into scan_sessions)" : "")
                                       .arg(reply.json["ingested"].toInt()).arg(reply.json["skipped"].toInt()).arg(problems),
                                   problems.isEmpty() ? UiTone::Neutral : UiTone::Warn);
            refresh();
        });
    }

    // The folder and the three fields a scan of the original controller was saved without. Not modal,
    // neither the dialog nor its folder chooser: Import stays available during a scan, and Stop and
    // Laser OFF must stay within reach while the dialog is open. Never exec(), which would also stall a
    // capture running behind it. A second press brings the open dialog to the front.
    QDialog* openImportDialog() {
        if (importDialog) {
            importDialog->raise();
            importDialog->activateWindow();
            return importDialog;
        }
        auto* dialog = new QDialog(dataTab->window());
        importDialog = dialog;
        dialog->setAttribute(Qt::WA_DeleteOnClose);
        dialog->setWindowModality(Qt::NonModal);
        dialog->setWindowTitle("Import scan folder");
        auto* form = new QGridLayout(dialog);
        form->setContentsMargins(16, 16, 16, 16); form->setHorizontalSpacing(8); form->setVerticalSpacing(8);
        auto* folderEdit = new QLineEdit; folderEdit->setObjectName("importFolder"); folderEdit->setMinimumWidth(380);
        folderEdit->setPlaceholderText("a folder with point_NNNN.json files");
        auto* chooseButton = new QPushButton("Choose...");
        auto* sampleEdit = new QLineEdit; sampleEdit->setObjectName("importSample");
        auto* operatorEdit = new QLineEdit; operatorEdit->setObjectName("importOperator");
        auto* locationEdit = new QLineEdit; locationEdit->setObjectName("importLocation");
        auto* note = new QLabel("Sample ID, Operator and Location are used only for points that were saved without them, "
                                "as the original controller did. Leave them empty for a folder of this edition.");
        note->setWordWrap(true); note->setObjectName("caption");
        auto* importNow = new QPushButton("Import"); importNow->setProperty("role", "primary"); importNow->setDefault(true);
        auto* cancelButton = new QPushButton("Cancel");
        int row = 0;
        const auto addRow = [&](const QString& name, QWidget* field, QWidget* extra = nullptr) {
            auto* label = new QLabel(name); label->setObjectName("fieldLabel");
            form->addWidget(label, row, 0);
            form->addWidget(field, row, 1, 1, extra ? 1 : 2);
            if (extra) form->addWidget(extra, row, 2);
            ++row;
        };
        addRow("Folder", folderEdit, chooseButton);
        addRow("Sample ID", sampleEdit);
        addRow("Operator", operatorEdit);
        addRow("Location", locationEdit);
        form->addWidget(note, row++, 1, 1, 2);
        auto* buttons = new QHBoxLayout;
        buttons->addStretch(); buttons->addWidget(cancelButton); buttons->addWidget(importNow);
        form->addLayout(buttons, row, 0, 1, 3);
        auto chooserShown = std::make_shared<QPointer<QFileDialog>>();
        connect(chooseButton, &QPushButton::clicked, dialog, [this, dialog, folderEdit, chooserShown] {
            if (*chooserShown) { (*chooserShown)->raise(); (*chooserShown)->activateWindow(); return; }
            auto* chooser = new QFileDialog(dialog, "Scan folder to import", dataLayer->homeDirectory() + "/scan_sessions");
            *chooserShown = chooser;
            chooser->setAttribute(Qt::WA_DeleteOnClose);
            chooser->setWindowModality(Qt::NonModal);
            chooser->setFileMode(QFileDialog::Directory);
            chooser->setOption(QFileDialog::ShowDirsOnly);
            connect(chooser, &QFileDialog::fileSelected, folderEdit, [folderEdit](const QString& folder) { folderEdit->setText(folder); });
            chooser->show();
        });
        connect(cancelButton, &QPushButton::clicked, dialog, &QDialog::reject);
        connect(importNow, &QPushButton::clicked, dialog, [this, dialog, folderEdit, sampleEdit, operatorEdit, locationEdit] {
            if (folderEdit->text().trimmed().isEmpty()) { folderEdit->setFocus(); return; }
            importFolder(folderEdit->text(), sampleEdit->text(), operatorEdit->text(), locationEdit->text());
            dialog->accept();
        });
        dialog->show();
        return dialog;
    }

    void updateControls() {
        if (!installed()) return;
        syncButton->setEnabled(isStarted && !syncRunning);
        classifyButton->setEnabled(isStarted && !classifyRunning);
        importButton->setEnabled(isStarted && !importRunning);
        offlineButton->setEnabled(isStarted && !offlineRunning);
        offlineButton->setText(catalogOffline() ? "Go online" : "Go offline");
        const QString reviewer = reviewerField->text().trimmed();
        const bool canReview = !reviewShownId.isEmpty() && !reviewer.isEmpty() && !reviewSetRunning;
        confirmButton->setEnabled(canReview);
        // For a spectrum the classifier rejected, Confirm records "unusable" already.
        rejectButton->setEnabled(canReview && !reviewShownRejected);
        // Without a class list from the data layer there is nothing to relabel to.
        relabelCombo->setEnabled(canReview && relabelCombo->count() > 0);
        relabelButton->setEnabled(canReview && relabelCombo->count() > 0);
        reviewerLabel->setText(reviewer.isEmpty() ? QString("Reviewer: fill in Operator first") : "Reviewer: " + reviewer);
        setStyleProperty(reviewerLabel, "tone", reviewer.isEmpty() ? QVariant("warn") : QVariant("dim"));
    }

    // ------------------------------------------------------------------ Data tab

    static QTableWidgetItem* cell(const QString& text, const QString& tip = QString()) {
        auto* item = new QTableWidgetItem(text);
        item->setToolTip(tip.isEmpty() ? text : tip);
        return item;
    }
    static QTableWidgetItem* sampleCell(const QJsonObject& item) {
        QTableWidgetItem* made = cell(DataText::sample(item));
        // Said in every row: a capture made with the simulators must not pass for a measurement.
        if (item["simulated"].toBool()) {
            made->setIcon(simulatedTag());
            made->setToolTip(DataText::sample(item) + "\nRecorded with the simulators, not with an instrument.");
        }
        return made;
    }
    static QLabel* paneTitle(const QString& text) {
        auto* label = new QLabel(text);
        label->setObjectName("paneTitle");
        return label;
    }
    static QWidget* notInstalledPage() {
        auto* page = new QWidget;
        auto* layout = new QVBoxLayout(page);
        auto* title = new QLabel("The data layer is not installed");
        title->setObjectName("emptyTitle"); title->setAlignment(Qt::AlignCenter);
        auto* text = new QLabel("This copy of the controller has no data_layer/raman_catalog folder,\n"
                                "so the Data and Review features are off and nothing is started for them.\n"
                                "Spectra are saved in scan_sessions as before.");
        text->setObjectName("emptyText"); text->setAlignment(Qt::AlignCenter);
        layout->addStretch(); layout->addWidget(title); layout->addWidget(text); layout->addStretch();
        return page;
    }

    QWidget* buildDataPage() {
        auto* page = new QWidget;
        auto* layout = new QVBoxLayout(page);
        layout->setContentsMargins(10, 8, 10, 10); layout->setSpacing(8);

        // Top left: which catalog is in use, how much is in it, what is wrong, what the last action did.
        auto* catalogRow = new QHBoxLayout; catalogRow->setSpacing(8);
        catalogBadge = new QLabel("DEMO"); catalogBadge->setObjectName("demoBadge"); catalogBadge->hide();
        catalogLabel = new QLabel("Asking the data layer which catalog is in use..."); catalogLabel->setWordWrap(true);
        catalogRow->addWidget(catalogBadge, 0, Qt::AlignTop); catalogRow->addWidget(catalogLabel, 1);
        summaryLabel = new ElidedLabel;
        problemsLabel = new ElidedLabel; problemsLabel->hide();
        activityLabel = new ElidedLabel;
        auto* texts = new QVBoxLayout; texts->setSpacing(2);
        texts->addLayout(catalogRow); texts->addWidget(summaryLabel); texts->addWidget(problemsLabel);
        texts->addWidget(activityLabel);
        texts->addStretch();

        // Top right: the actions.
        syncButton = new QPushButton("Sync now"); syncButton->setProperty("role", "primary");
        syncButton->setToolTip("Find new saved spectra, register them in the catalog and pick up predictions and reviews.\n"
                               "Also tries again what failed too often to be retried automatically.");
        classifyButton = new QPushButton("Classify now");
        classifyButton->setToolTip("Run the classifier on this Mac for the spectra that wait for it.");
        importButton = new QPushButton("Import scan folder");
        importButton->setToolTip("Add a scan folder that was recorded earlier, or by the original controller.");
        offlineButton = new QPushButton("Go offline");
        offlineButton->setToolTip("Local demo only: pretend the catalog cannot be reached. Captures then wait on this Mac.");
        // Below them, across both columns: the switch between the captures and the uploads to ARC.
        arcToggle = new QPushButton("ARC upload"); arcToggle->setObjectName("arcToggle"); arcToggle->setCheckable(true);
        arcToggle->setToolTip(kArcToggleTip);
        auto* actions = new QGridLayout; actions->setHorizontalSpacing(8); actions->setVerticalSpacing(6);
        actions->addWidget(syncButton, 0, 0); actions->addWidget(classifyButton, 0, 1);
        actions->addWidget(importButton, 1, 0); actions->addWidget(offlineButton, 1, 1);
        actions->addWidget(arcToggle, 2, 0, 1, 2);
        actions->setRowStretch(3, 1);
        connect(syncButton, &QPushButton::clicked, this, [this] { sync(true); });
        connect(classifyButton, &QPushButton::clicked, this, [this] { classify(); });
        connect(offlineButton, &QPushButton::clicked, this, [this] { setOffline(!catalogOffline()); });
        connect(importButton, &QPushButton::clicked, this, [this] { openImportDialog(); });
        auto* top = new QHBoxLayout; top->setSpacing(16);
        top->addLayout(texts, 1); top->addLayout(actions);

        // The prediction gets the largest share: "UNKNOWN (methylene_blue 0.80)" keeps its score at the default size
        // also beside the scroll bar of a 13-point scan.
        captureTable = new ShareTable({"Time", "Sample", "Point", "State", "Record", "Prediction", "Review"}, {0, 4, 0, 0, 0, 6, 2});
        connect(captureTable, &QTableWidget::itemSelectionChanged, this, [this] { showCaptureDetails(); });

        // Right of the table: the selected capture in full, then where it comes from. One scrolling column.
        auto* details = new QWidget;
        auto* detailsLayout = new QVBoxLayout(details);
        detailsLayout->setContentsMargins(12, 0, 6, 0); detailsLayout->setSpacing(4);
        captureFacts = new FactsText;
        captureProvenance = new ProvenanceTree;
        provenanceNote = new QLabel; provenanceNote->setObjectName("caption"); provenanceNote->setWordWrap(true);
        detailsLayout->addWidget(paneTitle("DETAILS"));
        detailsLayout->addWidget(captureFacts);
        detailsLayout->addSpacing(4);
        detailsLayout->addWidget(provenancePaneTitle = paneTitle("PROVENANCE (DERIVED FROM)"));
        detailsLayout->addWidget(provenanceNote);
        detailsLayout->addWidget(captureProvenance);
        detailsLayout->addStretch();
        auto* detailsScroll = new QScrollArea;
        detailsScroll->setWidget(details); detailsScroll->setWidgetResizable(true);
        detailsScroll->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
        detailsScroll->viewport()->setAutoFillBackground(false); details->setAutoFillBackground(false);
        detailsScroll->setMinimumWidth(200);

        auto* split = new DetailsSplitter;
        split->addWidget(captureTable); split->addWidget(detailsScroll);

        // The lower area: the captures with their details (shown first), or the ARC page while its
        // button is down. The ARC page starts nothing until it is first shown.
        arcPanel = new ArcPanel(dataLayer, this);
        arcPanel->countsChanged = [this](int unfinished, int attention) {
            arcUnfinished = unfinished;
            arcAttention = attention;
            colourArcToggle();
        };
        arcPanel->page()->installEventFilter(this);
        lowerStack = new QStackedWidget;
        lowerStack->addWidget(split); lowerStack->addWidget(arcPanel->page());
        connect(arcToggle, &QPushButton::toggled, this, [this](bool on) { lowerStack->setCurrentIndex(on ? 1 : 0); arcPageShown(); });

        layout->addLayout(top);
        layout->addWidget(lowerStack, 1);
        showProvenance(QJsonObject(), QString());
        return page;
    }

    // The ARC page reads and polls only while it is in view, and not before the panels have started.
    void arcPageShown() {
        if (arcPanel) arcPanel->setShown(isStarted && arcPanel->page()->isVisible());
    }
    // Uploads that need the person (failed, waiting for a decision, not watched, a failed check: status's
    // arc_attention) colour the ARC upload button in the danger tone, uploads that are not finished in the
    // warn tone: the button says that something waits there although the page is not in view. The counts
    // come from the status calls the window makes anyway, and from the ARC page, which reads them again at
    // the idle rate while it is hidden and the button has a colour (fix plan I2).
    void colourArcToggle() {
        if (!arcToggle) return;
        const UiTheme& theme = uiTheme();
        const bool needed = arcAttention > 0, waiting = !needed && arcUnfinished > 0;
        setStyleProperty(arcToggle, "tone", needed ? QVariant("danger") : waiting ? QVariant("warn") : QVariant());
        const QColor fill = needed ? theme.dangerFill : theme.warnFill, ink = needed ? theme.dangerInk : theme.warnInk;
        arcToggle->setStyleSheet(!needed && !waiting ? QString()
            : QString("QPushButton { background: %1; border-color: %2; color: %2; font-weight: 600; } "
                      "QPushButton:checked { border-width: 2px; padding: 2px 9px; }").arg(fill.name(), ink.name()));
        arcToggle->setToolTip(needed ? (arcAttention == 1 ? QString("1 upload to ARC needs you. Press to see it.")
                                                          : QString("%1 uploads to ARC need you. Press to see them.").arg(arcAttention))
                              : !waiting ? kArcToggleTip
                              : arcUnfinished == 1 ? QString("1 upload to ARC is not finished. Press to see it.")
                                                   : QString("%1 uploads to ARC are not finished. Press to see them.").arg(arcUnfinished));
    }

    QJsonObject selectedCapture() const {
        const QList<QTableWidgetItem*> selected = captureTable->selectedItems();
        return selected.isEmpty() ? QJsonObject() : captures.at(selected.first()->row()).toObject();
    }

    void showCaptures() {
        const QString keep = selectedCapture()["capture_id"].toString();
        int keptRow = -1;
        {
            const QSignalBlocker quiet(captureTable);
            captureTable->clearSelection();
            captureTable->setRowCount(int(captures.size()));
            const QFont stateFont = uiFont(captureTable, 12, QFont::Bold);
            for (int row = 0; row < captures.size(); ++row) {
                const QJsonObject item = captures.at(row).toObject();
                const QString state = item["state"].toString();
                const QString time = item["timestamp_utc"].toString();
                captureTable->setItem(row, 0, cell(DataText::localTime(time, "MM-dd HH:mm"), DataText::localTime(time, "yyyy-MM-dd HH:mm:ss")));
                captureTable->setItem(row, 1, sampleCell(item));
                captureTable->setItem(row, 2, cell(DataText::point(item)));
                auto* word = cell(item["state_label"].toString(state), item["error"].toObject()["message"].toString());
                word->setForeground(state == "captured" ? uiTheme().textDim : uiTheme().ink(DataText::stateTone(state)));
                word->setFont(stateFont);
                captureTable->setItem(row, 3, word);
                captureTable->setItem(row, 4, recordCell(item));
                captureTable->setItem(row, 5, cell(DataText::predictionCell(item), predictionTip(item)));
                captureTable->setItem(row, 6, cell(DataText::review(item["review"])));
                if (!keep.isEmpty() && item["capture_id"].toString() == keep) keptRow = row;
            }
            if (keptRow >= 0) captureTable->selectRow(keptRow);
        }
        captureTable->fitColumns();
        showCaptureDetails();
    }

    // A record id of the catalog used before the settings changed names nothing in this one: it is only
    // in the tool tip, until the next sync has registered the capture here.
    QTableWidgetItem* recordCell(const QJsonObject& item) const {
        const QString record = item["record_id"].toString();
        if (!catalogChanged() || record.isEmpty()) return cell(record.isEmpty() ? DataText::none() : record);
        return cell(DataText::none(), QString("%1 in the catalog used before the settings changed; the next sync registers this capture in %2.")
                                          .arg(record, "the demo catalog"));
    }
    // The model's own record behind the worded cell: its label, its outcome, and the review.
    static QString predictionTip(const QJsonObject& item) {
        if (!item["prediction"].isObject()) return QString();
        const QJsonObject prediction = item["prediction"].toObject();
        QStringList lines{"Model: " + uiJoin({DataText::modelLabel(prediction), prediction["outcome"].toString().replace('_', ' ')})};
        if (DataText::decided(item["review"].toObject()["status"].toString())) lines << "Review: " + DataText::review(item["review"]);
        return lines.join("\n");
    }

    QString captureFactsText(const QJsonObject& item) const {
        QStringList rows;
        const auto row = [&](const QString& name, const QString& value) {
            if (value.isEmpty()) return;
            rows << QString("<tr><td style='color:%1;padding-right:10px;white-space:nowrap;'>%2</td><td>%3</td></tr>")
                        .arg(uiTheme().textDim.name(), name, value.toHtmlEscaped());
        };
        row("Sample", uiJoin({DataText::sample(item), item["scan_mode"].toString() == "single" ? QString("single capture")
                                                                           : QString("point %1").arg(DataText::point(item))})
                      + (item["simulated"].toBool() ? "  (simulated, no instrument)" : ""));
        row("Taken", DataText::localTime(item["timestamp_utc"].toString(), "yyyy-MM-dd HH:mm:ss")
                     + (item["operator"].toString().isEmpty() ? QString() : " by " + item["operator"].toString()));
        row("Session", item["session_folder"].toString());
        if (item["position_mm"].isObject()) {
            const QJsonObject at = item["position_mm"].toObject();
            row("Position", QString("x %1, y %2, z %3 mm").arg(at["x"].toDouble(), 0, 'f', 2).arg(at["y"].toDouble(), 0, 'f', 2).arg(at["z"].toDouble(), 0, 'f', 2));
        }
        QString state = item["state_label"].toString(item["state"].toString());
        if (item["error"].isObject()) {
            const QJsonObject error = item["error"].toObject();
            state += QString(" (%1): %2").arg(error["stage"].toString(), error["message"].toString());
        }
        row("State", state);
        // Records of the catalog used before the settings changed are said to be that catalog's.
        const QString previous = catalogChanged() ? QString("in the catalog used before: ") : QString();
        if (!item["record_id"].toString().isEmpty()) {
            QStringList record{item["record_id"].toString() + "  (" + item["record_alias"].toString() + ")"};
            if (item["upload"].isObject()) record << "upload " + item["upload"].toObject()["status"].toString();
            row(!catalogChanged() ? "Demo record" : "Record", previous + uiJoin(record));
        }
        if (item["prediction"].isObject()) {
            const QJsonObject prediction = item["prediction"].toObject();
            row("Prediction", previous + uiJoin({DataText::modelLabel(prediction), prediction["outcome"].toString().replace('_', ' '),
                                                 prediction["record_id"].toString()}));
        }
        if (item["review"].isObject()) {
            const QJsonObject review = item["review"].toObject();
            QStringList parts{DataText::review(review)};
            if (!review["reviewer"].toString().isEmpty()) parts << "by " + review["reviewer"].toString();
            if (!review["time_utc"].toString().isEmpty()) parts << DataText::localTime(review["time_utc"].toString(), "yyyy-MM-dd HH:mm");
            row("Review", uiJoin(parts));
        }
        row("Capture id", item["capture_id"].toString());
        return "<table cellspacing='0' cellpadding='0'>" + rows.join(QString()) + "</table>";
    }

    void showCaptureDetails() {
        const QJsonObject item = selectedCapture();
        if (item.isEmpty()) {
            captureFacts->setFacts(QString("<span style='color:%1;'>%2</span>").arg(uiTheme().textDim.name(),
                captures.isEmpty() ? QString("No captures yet. Saved spectra appear here after the next sync.")
                                   : QString("Select a capture to see its details and where it comes from.")));
            provenanceShownFor.clear();
            wantProvenance(QString());
            return;
        }
        captureFacts->setFacts(captureFactsText(item));
        // Asked again only when the chain can have changed: a record was made, a prediction or a review was
        // added, the rows moved to the catalog in use.
        const QString key = item["record_id"].toString() + "|" + item["prediction"].toObject()["record_id"].toString()
                            + "|" + item["review"].toObject()["status"].toString() + (catalogChanged() ? "|previous" : "");
        if (key == provenanceShownFor) return;
        provenanceShownFor = key;
        wantProvenance(item["record_id"].toString());
    }

    // The newest request wins: an answer for a row the selection has left is dropped.
    void wantProvenance(const QString& record) {
        // The catalog in use is not asked about a record id of the one used before: it names nothing there.
        provenanceWanted = catalogChanged() ? QString() : record;
        if (catalogChanged() && !record.isEmpty()) {
            showProvenance(QJsonObject(), QString("Registered in the catalog used before the settings changed. The provenance shows once "
                                                  "the next sync has registered it in the demo catalog."));
            return;
        }
        if (record.isEmpty()) {
            showProvenance(QJsonObject(), selectedCapture().isEmpty() ? QString()
                : QString("Not registered in the demo catalog yet, so there is no provenance to show."));
            return;
        }
        if (!provenanceRunning) fetchProvenance();
    }
    void fetchProvenance() {
        const QString record = provenanceWanted;
        provenanceRunning = true;
        dataLayer->run({"provenance", "--record", record}, [this, record](const DataLayerReply& reply) {
            provenanceRunning = false;
            if (record != provenanceWanted) {
                if (!provenanceWanted.isEmpty()) fetchProvenance();
                return;
            }
            if (reply.ok()) showProvenance(reply.json, QString());
            else {
                provenanceShownFor.clear();
                showProvenance(QJsonObject(), failureText("The provenance could not be read", reply));
            }
        });
    }
    void showProvenance(const QJsonObject& graph, const QString& note) {
        captureGraph = graph;
        captureProvenance->setGraph(graph);
        const int rows = captureProvenance->rowCount();
        captureProvenance->setVisible(rows > 0);
        captureProvenance->setFixedHeight(captureProvenance->heightForRows(std::min(rows, 8)));
        const QString text = !note.isEmpty() || graph.isEmpty() || captureProvenance->hasDerivedRecord() ? note
                           : QString("Nothing has been derived from this record yet.");
        provenanceNote->setText(text);
        provenanceNote->setVisible(!text.isEmpty());
        provenancePaneTitle->setVisible(!text.isEmpty() || rows > 0);
    }

    // ------------------------------------------------------------------ Review tab

    QWidget* buildReviewPage() {
        auto* page = new QWidget;
        auto* layout = new QHBoxLayout(page);
        layout->setContentsMargins(10, 8, 10, 10); layout->setSpacing(14);

        // Left: what waits, and where the selected prediction comes from.
        auto* listColumn = new QVBoxLayout; listColumn->setSpacing(4);
        reviewListTitle = paneTitle("WAITING FOR REVIEW");
        reviewTable = new ShareTable({"Sample", "Point", "Top class"}, {1, 0, 0});
        connect(reviewTable, &QTableWidget::itemSelectionChanged, this, [this] { reviewSelectionChanged(); });
        reviewProvenance = new ProvenanceTree;
        // A prediction and the three records it is derived from: the spectrum, the model, the dark.
        reviewProvenance->setFixedHeight(reviewProvenance->heightForRows(4));
        listColumn->addWidget(reviewListTitle); listColumn->addWidget(reviewTable, 1);
        listColumn->addSpacing(4);
        listColumn->addWidget(paneTitle("PROVENANCE (DERIVED FROM)")); listColumn->addWidget(reviewProvenance);
        auto* listBox = new QWidget; listBox->setLayout(listColumn); listBox->setMinimumWidth(336);
        listColumn->setContentsMargins(0, 0, 0, 0);

        // Middle: the spectrum as the classifier was given it.
        reviewSeries = new QLineSeries;
        auto* chart = new QChart; chart->addSeries(reviewSeries); chart->legend()->hide();
        chart->setMargins(QMargins(4, 2, 8, 2));
        reviewAxisX = new QValueAxis; reviewAxisY = new QValueAxis;
        reviewAxisY->setTitleText("Classifier input");
        reviewAxisX->setLabelFormat("%.0f"); reviewAxisY->setLabelFormat("%.2f");
        chart->addAxis(reviewAxisX, Qt::AlignBottom); chart->addAxis(reviewAxisY, Qt::AlignLeft);
        reviewSeries->attachAxis(reviewAxisX); reviewSeries->attachAxis(reviewAxisY);
        useRoundTicks(chart, reviewAxisX, Qt::Horizontal); useRoundTicks(chart, reviewAxisY, Qt::Vertical);
        reviewChartView = new QChartView(chart);
        reviewChartView->setRenderHint(QPainter::Antialiasing);
        reviewChartView->setFrameShape(QFrame::NoFrame);
        reviewChartView->setMinimumSize(240, 110);
        auto* seenColumn = new QVBoxLayout; seenColumn->setSpacing(2); seenColumn->setContentsMargins(0, 0, 0, 0);
        seenColumn->addWidget(spectrumTitle = paneTitle("WHAT THE CLASSIFIER SAW")); seenColumn->addWidget(reviewChartView, 1);

        // Right: what the classifier concluded, and why a person has to look.
        auto* scoreColumn = new QVBoxLayout; scoreColumn->setSpacing(4); scoreColumn->setContentsMargins(0, 0, 0, 0);
        scoresTitle = paneTitle("SCORES");
        scoreBars = new ScoreBars;
        reviewReason = new QLabel; reviewReason->setWordWrap(true); reviewReason->setTextFormat(Qt::RichText);
        reviewReason->setAlignment(Qt::AlignTop | Qt::AlignLeft);
        scoreColumn->addWidget(scoresTitle); scoreColumn->addWidget(scoreBars);
        scoreColumn->addSpacing(6);
        reviewProblems = new QLabel; reviewProblems->setWordWrap(true); reviewProblems->setProperty("tone", "warn");
        scoreColumn->addWidget(paneTitle("WHY IT NEEDS REVIEW")); scoreColumn->addWidget(reviewReason);
        scoreColumn->addWidget(reviewProblems); scoreColumn->addStretch(1);
        auto* scoreBox = new QWidget; scoreBox->setLayout(scoreColumn); scoreBox->setMinimumWidth(196);
        auto* seen = new QHBoxLayout; seen->setSpacing(14);
        seen->addLayout(seenColumn, 3); seen->addWidget(scoreBox, 2);

        // Below: the decision. The reviewer is the Operator of the Sample group.
        // Confirm names the class it records once a prediction is shown (showReview).
        confirmButton = new QPushButton("Confirm"); confirmButton->setProperty("role", "primary");
        relabelButton = new QPushButton("Relabel");
        relabelButton->setToolTip("Record the class chosen on the right as the correct one; \"unknown\" for a material that is none of them.");
        relabelCombo = new QComboBox; relabelCombo->setMinimumWidth(124);
        relabelCombo->setToolTip("The class to relabel to.");
        rejectButton = new QPushButton("Mark unusable"); rejectButton->setProperty("role", "stop");
        // The comment is written first and then recorded by one of the buttons below it. It has the whole
        // width: beside the buttons it had room for a few words only at the smallest window size.
        commentEdit = new QLineEdit; commentEdit->setPlaceholderText("Comment (optional)");
        // The two verdicts on one row, Relabel with its class and the reviewer on the next: with the class
        // in its text, Confirm takes room that one row does not have at the smaller window sizes.
        reviewerLabel = new QLabel; reviewerLabel->setObjectName("caption");
        auto* verdictRow = new QHBoxLayout; verdictRow->setSpacing(8);
        verdictRow->addWidget(confirmButton); verdictRow->addWidget(rejectButton); verdictRow->addStretch(1);
        auto* relabelRow = new QHBoxLayout; relabelRow->setSpacing(8);
        relabelRow->addWidget(relabelButton); relabelRow->addWidget(relabelCombo); relabelRow->addStretch(1); relabelRow->addWidget(reviewerLabel);
        connect(confirmButton, &QPushButton::clicked, this, [this] { submitReview("confirm"); });
        connect(relabelButton, &QPushButton::clicked, this, [this] { submitReview("relabel"); });
        connect(rejectButton, &QPushButton::clicked, this, [this] { submitReview("reject"); });
        // The result has the whole width: where the review is comes first, and it changes once a later sync has sent it.
        reviewResult = new ElidedLabel;

        auto* item = new QWidget;
        auto* itemLayout = new QVBoxLayout(item);
        itemLayout->setContentsMargins(0, 0, 0, 0); itemLayout->setSpacing(6);
        itemLayout->addLayout(seen, 1);
        itemLayout->addWidget(commentEdit);
        itemLayout->addLayout(verdictRow);
        itemLayout->addLayout(relabelRow);
        itemLayout->addWidget(reviewResult);

        reviewEmpty = new QLabel("Nothing is waiting for review.");
        reviewEmpty->setObjectName("emptyText"); reviewEmpty->setAlignment(Qt::AlignCenter); reviewEmpty->setWordWrap(true);
        reviewStack = new QStackedLayout;
        reviewStack->addWidget(reviewEmpty); reviewStack->addWidget(item);

        layout->addWidget(listBox, 4);
        layout->addLayout(reviewStack, 9);
        return page;
    }

    QJsonObject selectedReviewItem() const {
        const QList<QTableWidgetItem*> selected = reviewTable->selectedItems();
        return selected.isEmpty() ? QJsonObject() : reviewItems.at(selected.first()->row()).toObject();
    }

    void showReviewList() {
        const QString keep = selectedReviewItem()["prediction_id"].toString();
        int keptRow = -1;
        {
            const QSignalBlocker quiet(reviewTable);
            reviewTable->clearSelection();
            reviewTable->setRowCount(int(reviewItems.size()));
            for (int row = 0; row < reviewItems.size(); ++row) {
                const QJsonObject item = reviewItems.at(row).toObject();
                const QJsonObject prediction = item["prediction"].toObject();
                const QString taken = "Taken " + DataText::localTime(item["timestamp_utc"].toString(), "yyyy-MM-dd HH:mm:ss");
                QTableWidgetItem* sample = sampleCell(item);
                sample->setToolTip(sample->toolTip() + "\n" + taken);
                reviewTable->setItem(row, 0, sample);
                reviewTable->setItem(row, 1, cell(DataText::point(item), taken));
                reviewTable->setItem(row, 2, cell(prediction["outcome"].toString() == "rejected" ? QString("rejected")
                                                                                                 : DataText::topScore(prediction)));
                if (!keep.isEmpty() && item["prediction_id"].toString() == keep) keptRow = row;
            }
            // Something is always selected while anything waits, so the tab never opens on an empty half.
            if (!reviewItems.isEmpty()) reviewTable->selectRow(keptRow >= 0 ? keptRow : 0);
        }
        reviewTable->fitColumns();
        reviewListTitle->setText(QString("WAITING FOR REVIEW (%1)").arg(reviewItems.size()));
        // An item that was shown while the catalog could not be reached is asked for again once it can:
        // its scores and provenance come then, and the pane no longer says that the catalog is away.
        // An empty list has no such item, also when the review that emptied it was made offline.
        if (reviewShownWithoutCatalog && !reviewShownId.isEmpty() && status["online"].toBool() && reviewWanted.isEmpty() &&
            selectedReviewItem()["prediction_id"].toString() == reviewShownId) {
            reviewWanted = reviewShownId;
            if (!reviewShowRunning) fetchReview();
            return;
        }
        reviewSelectionChanged();
    }

    void reviewSelectionChanged() {
        const QString prediction = selectedReviewItem()["prediction_id"].toString();
        if (prediction.isEmpty()) {
            reviewWanted.clear(); reviewShownId.clear();
            reviewShownWithoutCatalog = false;
            reviewGraph = QJsonObject();
            reviewProvenance->setGraph(reviewGraph);
            // The result of the review that emptied the list stays readable here.
            const QString recorded = reviewResult->tone() == UiTone::Ok ? reviewResult->text() + "\n" : QString();
            reviewEmpty->setText(statusProblem.isEmpty() ? recorded + "Nothing is waiting for review." : statusProblem);
            reviewStack->setCurrentIndex(0);
            updateControls();
            return;
        }
        if (prediction == reviewShownId || prediction == reviewWanted) return;
        reviewWanted = prediction;
        if (!reviewShowRunning) fetchReview();
    }
    void fetchReview() {
        const QString prediction = reviewWanted;
        reviewShowRunning = true;
        dataLayer->run({"review-show", "--prediction", prediction}, [this, prediction](const DataLayerReply& reply) {
            reviewShowRunning = false;
            if (prediction != reviewWanted) {
                if (!reviewWanted.isEmpty()) fetchReview();
                return;
            }
            reviewWanted.clear();
            if (reply.ok()) showReview(prediction, reply.json);
            else {
                reviewShownId.clear();
                reviewShownWithoutCatalog = false;
                reviewEmpty->setText(failureText("This prediction could not be shown", reply));
                reviewStack->setCurrentIndex(0);
            }
            updateControls();
        });
    }

    // The classifier states its reasons as phrases ("no dark spectrum", "Low raw signal"); here they stand as sentences.
    static QString sentence(const QString& phrase) {
        QString text = phrase.simplified();
        if (text.isEmpty()) return text;
        text[0] = text[0].toUpper();
        if (!text.endsWith('.') && !text.endsWith('!') && !text.endsWith('?')) text += '.';
        return text.toHtmlEscaped();
    }
    static QString reviewReasonText(const QJsonObject& item) {
        const QJsonObject prediction = item["prediction"].toObject();
        const QString outcome = prediction["outcome"].toString(), reason = sentence(item["reason"].toString());
        if (outcome == "rejected")
            return "<b>Rejected by the classifier.</b> " + (reason.isEmpty() ? QString("The spectrum could not be used.") : reason);
        if (outcome != "low_confidence") return reason;
        // The reason names the rule ("Low pure-class confidence"), the numbers follow. Offline neither the
        // reason nor the threshold is known here; the text then does without them.
        const QString limit = item["threshold"].isDouble() ? " " + DataText::score(item["threshold"].toDouble()) : QString();
        return QString("<b>%1</b> The top score, %2 for %3, is below the threshold%4.")
            .arg(reason.isEmpty() ? QString("Low confidence.") : reason, DataText::confidence(prediction),
                 prediction["top_class"].toString().toHtmlEscaped(), limit);
    }

    // review-show lists the records a prediction is derived from. The tree takes the chain form of
    // the provenance command, so the prediction is put in front as the record the chain starts from.
    static QJsonObject reviewChain(const QJsonObject& item, const QJsonArray& sources) {
        if (sources.isEmpty()) return QJsonObject();
        const QJsonValue prediction = item["prediction_id"];
        QJsonArray nodes{QJsonObject{{"id", prediction}, {"type", "raman.prediction"}, {"role", "start"}, {"info", item["prediction"]}}};
        QJsonArray edges;
        for (const QJsonValue& value : sources) {
            QJsonObject source = value.toObject();
            edges.append(QJsonObject{{"from", prediction}, {"to", source["id"]}, {"relation", source["relation"]}, {"label", source["label"]}});
            source["role"] = "source";
            nodes.append(source);
        }
        return QJsonObject{{"nodes", nodes}, {"edges", edges}};
    }

    void showReview(const QString& prediction, const QJsonObject& shown) {
        const QJsonObject item = shown["item"].toObject();
        reviewShownId = prediction;
        reviewShownWithoutCatalog = shown["online"].isBool() && !shown["online"].toBool();
        QStringList about{DataText::sample(item) + (item["simulated"].toBool() ? " (simulated)" : ""),
                          item["scan_mode"].toString() == "single" ? QString("single") : QString("point %1").arg(DataText::point(item))};
        showReviewSpectrum(shown["spectrum"].toObject(), uiJoin(about), item["prediction"].toObject()["outcome"].toString() == "rejected");
        scoreBars->setScores(item["scores"].toObject(), item["threshold"].toDouble());
        scoresTitle->setText(item["threshold"].isDouble() ? QString("SCORES (THRESHOLD %1)").arg(DataText::score(item["threshold"].toDouble()))
                                                          : QString("SCORES"));
        reviewReason->setText(reviewReasonText(item));
        // What the data layer could not show, and why (the catalog cannot be reached, a file is gone).
        QStringList problems;
        for (const QJsonValue& problem : shown["problems"].toArray()) problems << problem.toString();
        reviewProblems->setText(problems.join(" "));
        reviewProblems->setVisible(!problems.isEmpty());
        reviewGraph = reviewChain(item, shown["provenance"].toArray());
        reviewProvenance->setGraph(reviewGraph);
        const QString top = item["prediction"].toObject()["top_class"].toString();
        // The buttons say what they record: review-set stores the top class for confirm, and for a
        // spectrum the classifier rejected it stores that verdict, which is what Mark unusable would record.
        reviewShownRejected = item["prediction"].toObject()["outcome"].toString() == "rejected";
        reviewShownTop = top;
        confirmButton->setText(reviewShownRejected ? QString("Confirm: unusable") : top.isEmpty() ? QString("Confirm") : "Confirm " + top);
        confirmButton->setToolTip(reviewShownRejected ? QString("Record that this spectrum cannot be used, as the classifier found.")
                                  : QString("Record %1 as the class of this spectrum.").arg(top.isEmpty() ? QString("the top class") : top));
        rejectButton->setToolTip(reviewShownRejected ? QString("The classifier rejected this spectrum already: Confirm records that it cannot be used.")
                                                     : QString("Record that this spectrum cannot be used, whatever the scores say."));
        relabelCombo->clear();
        for (const QJsonValue& name : shown["classes"].toArray()) relabelCombo->addItem(name.toString());
        // The data layer also takes "unknown": a usable spectrum of a material that is none of the classes.
        if (relabelCombo->count() > 0) relabelCombo->addItem("unknown");
        if (relabelCombo->findText(top) >= 0) relabelCombo->setCurrentIndex(relabelCombo->findText(top));
        reviewStack->setCurrentIndex(1);
    }

    // "processed" is what the classifier was given: index-aligned with the Raman shifts, and null where
    // the model does not look (its first pixels). A rejected spectrum was given to no model, although
    // review-show still computes that curve for it when it can (a flat signal makes a flat line): the
    // raw spectrum is shown then, and said to be raw, as it is when review-show has no such curve.
    void showReviewSpectrum(const QJsonObject& spectrum, const QString& what, bool rejected) {
        const QJsonArray processed = rejected ? QJsonArray() : spectrum["processed"].toArray();
        bool raw = true;
        for (const QJsonValue& value : processed) raw = raw && !value.isDouble();
        const QJsonArray values = raw ? spectrum["intensity"].toArray() : processed;
        // Short enough for the narrow column of the smallest window, which a longer title would widen.
        spectrumTitle->setText(values.isEmpty() ? "NO SPECTRUM TO SHOW" : !raw ? "WHAT THE CLASSIFIER SAW"
                               : rejected ? "RAW SPECTRUM (NOT CLASSIFIED)" : "RAW SPECTRUM");
        reviewAxisY->setTitleText(raw ? "Intensity" : "Classifier input");
        reviewAxisY->setLabelFormat(raw ? "%.0f" : "%.2f");
        const QJsonArray shift = spectrum["raman_shift"].toArray();
        const bool byShift = !values.isEmpty() && shift.size() == values.size();
        QList<QPointF> points;
        points.reserve(values.size());
        double low = std::numeric_limits<double>::max(), high = std::numeric_limits<double>::lowest();
        for (int i = 0; i < values.size(); ++i) {
            if (!values.at(i).isDouble()) continue;
            const double y = values.at(i).toDouble();
            points.append(QPointF(byShift ? shift.at(i).toDouble() : i, y));
            low = std::min(low, y); high = std::max(high, y);
        }
        reviewSeries->replace(points);
        reviewChartView->chart()->setTitle(what);
        reviewAxisX->setTitleText(byShift ? QStringLiteral("Raman shift (cm\u207B\u00B9)") : QString("Pixel"));
        if (points.isEmpty()) return;
        reviewAxisX->setRange(points.first().x(), points.last().x());
        const double margin = high > low ? (high - low) * 0.05 : 1.0;
        reviewAxisY->setRange(low - margin, high + margin);
    }

    void submitReview(const QString& decision) {
        const QString reviewer = reviewerField->text().trimmed();
        if (reviewShownId.isEmpty() || reviewer.isEmpty() || reviewSetRunning) return;
        if (decision == "reject" && reviewShownRejected) return;
        const QString prediction = reviewShownId;
        QStringList command{"review-set", "--prediction", prediction, "--decision", decision};
        if (decision == "relabel") command << "--label" << relabelCombo->currentText();
        command << option("--reviewer", reviewer);
        if (!commentEdit->text().trimmed().isEmpty()) command << option("--comment", commentEdit->text().trimmed());
        // What the decision records, in the words of its button (review-set, PROTOCOL.md section 6).
        const QString top = DataText::reviewLabel(reviewShownRejected ? QString("REJECTED") : reviewShownTop);
        const QString done = decision == "reject" ? QString("marked unusable")
                           : decision == "relabel" ? "relabelled as " + DataText::reviewLabel(relabelCombo->currentText())
                           : top.isEmpty() ? QString("confirmed") : "confirmed as " + top;
        reviewSetRunning = true;
        reviewResult->setText("Recording the review...");
        updateControls();
        dataLayer->run(command, [this, prediction, done](const DataLayerReply& reply) {
            reviewSetRunning = false;
            if (reply.ok()) {
                resultFor = prediction;
                resultDecision = done;
                const bool kept = reply.json["queued"].toBool();
                if (kept) reviewsKept.insert(prediction);
                else reviewsKept.remove(prediction);
                const QString where = kept ? "Kept on this Mac until the demo catalog is online" : "Recorded in the demo catalog";
                reviewResult->setText(reviewedText(where, prediction, done), UiTone::Ok);
                commentEdit->clear();
                reviewShownId.clear();
                reviewShownWithoutCatalog = false;
                refresh();
            } else {
                reviewResult->setText(failureText("The review was not recorded", reply), UiTone::Danger);
            }
            updateControls();
        });
    }
    // "<where the review is>: <prediction> <what was decided>." Where comes first: it is the part that
    // changes, and it stays in view when the line is cut short.
    static QString reviewedText(const QString& where, const QString& prediction, const QString& done) {
        return QString("%1: %2 %3.").arg(where, prediction, done);
    }
    // D4: a review kept on this Mac is not said to be there any more once a sync has sent it. The rows
    // say so: their review loses "queued". The result line about it, also where it stands in for the
    // empty list, then says when it went.
    void noteSentReviews() {
        if (reviewsKept.isEmpty()) return;
        for (const QJsonValue& value : captures) {
            const QJsonObject item = value.toObject();
            const QString prediction = item["prediction"].toObject()["record_id"].toString();
            if (!reviewsKept.contains(prediction) || !item["review"].isObject() || item["review"].toObject()["queued"].toBool()) continue;
            reviewsKept.remove(prediction);
            if (prediction != resultFor || reviewResult->tone() != UiTone::Ok) continue;
            const QDateTime synced = QDateTime::fromString(status["last_sync_utc"].toString(), Qt::ISODateWithMs);
            const QString at = (synced.isValid() ? synced.toLocalTime() : QDateTime::currentDateTime()).toString("HH:mm");
            reviewResult->setText(reviewedText(QString("Recorded in the demo catalog at %1").arg(at),
                                               prediction, resultDecision), UiTone::Ok);
        }
    }

    DataLayerClient* dataLayer = nullptr;
    StudioLauncher* studio = nullptr;
    QLineEdit* reviewerField = nullptr;
    QTimer autoSync;
    QElapsedTimer lastRefresh;
    bool isStarted = false, acquisitionBusy = false, syncDue = false;
    bool refreshRunning = false, refreshAgain = false, syncRunning = false, syncAgain = false;
    bool classifyRunning = false, offlineRunning = false, importRunning = false;
    bool provenanceRunning = false, reviewShowRunning = false, reviewSetRunning = false;
    bool reviewShownWithoutCatalog = false;    // review-show answered from this Mac alone ("online": false)
    QJsonObject status;           // the latest "status" object; empty until the first answer
    QString statusProblem;        // why there is no status, when the last attempt failed
    QJsonArray captures, reviewItems;
    int captureTotal = 0;
    QString provenanceWanted, provenanceShownFor, reviewWanted, reviewShownId;
    QJsonObject captureGraph, reviewGraph;    // the chains on show, kept to draw them again in other colours
    bool reviewShownRejected = false;         // the prediction on show is one the classifier rejected
    QString reviewShownTop;                   // and its top class, which Confirm records
    QSet<QString> reviewsKept;                // predictions whose review is kept on this Mac until a sync sends it
    QString resultFor, resultDecision;        // the prediction the review result line is about, and what was decided
    QPointer<QDialog> importDialog;
    ArcPanel* arcPanel = nullptr;             // the ARC page; none without a data layer
    QPushButton* arcToggle = nullptr;
    QStackedWidget* lowerStack = nullptr;     // the captures (page 0) or the ARC page (page 1)
    int arcUnfinished = 0;                    // uploads to ARC in a working state, uncertain or conflict
    int arcAttention = 0;                     // uploads to ARC that need the person (status.arc_attention)

    QWidget *dataTab = nullptr, *reviewTab = nullptr;
    QLabel *catalogBadge = nullptr, *catalogLabel = nullptr, *provenanceNote = nullptr, *provenancePaneTitle = nullptr;
    ElidedLabel *summaryLabel = nullptr, *problemsLabel = nullptr, *activityLabel = nullptr, *reviewResult = nullptr;
    QPushButton *syncButton = nullptr, *classifyButton = nullptr, *offlineButton = nullptr, *importButton = nullptr;
    ShareTable *captureTable = nullptr, *reviewTable = nullptr;
    FactsText* captureFacts = nullptr;
    ProvenanceTree *captureProvenance = nullptr, *reviewProvenance = nullptr;
    QLabel *reviewListTitle = nullptr, *spectrumTitle = nullptr, *scoresTitle = nullptr, *reviewReason = nullptr;
    QLabel *reviewerLabel = nullptr, *reviewEmpty = nullptr, *reviewProblems = nullptr;
    QStackedLayout* reviewStack = nullptr;
    QLineSeries* reviewSeries = nullptr;
    QValueAxis *reviewAxisX = nullptr, *reviewAxisY = nullptr;
    QChartView* reviewChartView = nullptr;
    ScoreBars* scoreBars = nullptr;
    QLineEdit* commentEdit = nullptr;
    QComboBox* relabelCombo = nullptr;
    QPushButton *confirmButton = nullptr, *relabelButton = nullptr, *rejectButton = nullptr;
};
