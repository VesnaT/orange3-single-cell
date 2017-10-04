import re
import sys

import numpy as np

from AnyQt.QtWidgets import QFormLayout, QApplication
from AnyQt.QtGui import QPainter
from AnyQt.QtCore import Qt, QTimer

import Orange.data
from Orange.data import Domain, Table, ContinuousVariable
import Orange.projection
import Orange.distance
import Orange.misc
from Orange.widgets import gui, settings
from Orange.widgets.settings import SettingProvider
from Orange.widgets.utils.sql import check_sql_input
from Orange.canvas import report
from Orange.widgets.visualize.owscatterplotgraph import OWScatterPlotGraph, InteractiveViewBox
from Orange.widgets.widget import Msg, OWWidget, Input, Output
from Orange.widgets.utils.annotated_data import (create_annotated_table,
                                                 ANNOTATED_DATA_SIGNAL_NAME)


RE_FIND_INDEX = r"(^{} \()(\d{{1,}})(\)$)"


def get_indices(names, name):
    """
    Return list of indices which occur in a names list for a given name.
    :param names: list of strings
    :param name: str
    :return: list of indices
    """
    return [int(a.group(2)) for x in names
            for a in re.finditer(RE_FIND_INDEX.format(name), x)]


def get_unique_names(names, proposed):
    """
    Returns unique names of variables. Variables which are duplicate get appended by
    unique index which is the same in all proposed variable names in a list.
    :param names: list of strings
    :param proposed: list of strings
    :return: list of strings
    """
    if len([name for name in proposed if name in names]):
        max_index = max([max(get_indices(names, name),
                             default=1) for name in proposed], default=1)
        for i, name in enumerate(proposed):
            proposed[i] = "{} ({})".format(name, max_index + 1)
    return proposed


class MDSInteractiveViewBox(InteractiveViewBox):
    def _dragtip_pos(self):
        return 10, 10


class OWMDSGraph(OWScatterPlotGraph):
    jitter_size = settings.Setting(0)

    def __init__(self, scatter_widget, parent=None, name="None", view_box=None):
        super().__init__(scatter_widget, parent=parent, _=name,
                         view_box=view_box)
        for axis_loc in ["left", "bottom"]:
            self.plot_widget.hideAxis(axis_loc)

    def update_data(self, attr_x, attr_y, reset_view=True):
        super().update_data(attr_x, attr_y, reset_view=reset_view)
        for axis in ["left", "bottom"]:
            self.plot_widget.hideAxis(axis)
        self.plot_widget.setAspectLocked(True, 1)

    def get_size_index(self):
        if self.attr_size == "Stress":
            return -2
        return super().get_size_index()

    def compute_sizes(self):
        def scale(a):
            dmin, dmax = np.nanmin(a), np.nanmax(a)
            if dmax - dmin > 0:
                return (a - dmin) / (dmax - dmin)
            else:
                return np.zeros_like(a)

        self.master.Information.missing_size.clear()
        size_index = self.get_size_index()
        if size_index == -1:
            size_data = np.full((self.n_points,), self.point_width,
                                dtype=float)
        elif size_index == -2:
            size_data = scale(stress(self.master.embedding, self.master.effective_matrix))
            size_data = self.MinShapeSize + size_data * self.point_width
        else:
            size_data = \
                self.MinShapeSize + \
                self.scaled_data[size_index, self.valid_data] * \
                self.point_width
        nans = np.isnan(size_data)
        if np.any(nans):
            size_data[nans] = self.MinShapeSize - 2
            self.master.Information.missing_size(self.attr_size)
        return size_data


#: Maximum number of displayed closest pairs.
MAX_N_PAIRS = 10000

class OWtSNE(OWWidget):
    name = "t-SNE"
    description = "Two-dimensional data projection with t-SNE."
    icon = "icons/TSNE.svg"

    class Inputs:
        data = Input("Data", Orange.data.Table, default=True)
        data_subset = Input("Data Subset", Orange.data.Table)

    class Outputs:
        selected_data = Output("Selected Data", Orange.data.Table, default=True)
        annotated_data = Output(ANNOTATED_DATA_SIGNAL_NAME, Orange.data.Table)

    settings_version = 2

    #: Initialization type
    PCA, Random = 0, 1

    #: Refresh rate
    RefreshRate = [
        ("Every iteration", 1),
        ("Every 5 steps", 5),
        ("Every 10 steps", 10),
        ("Every 25 steps", 25),
        ("Every 50 steps", 50),
        ("None", -1)
    ]

    #: Runtime state
    Running, Finished, Waiting = 1, 2, 3

    settingsHandler = settings.DomainContextHandler()

    max_iter = settings.Setting(1000)
    perplexity = settings.Setting(30)
    initialization = settings.Setting(PCA)
    pca_components = settings.Setting(10)
    refresh_rate = settings.Setting(5)

    # output embedding role.
    NoRole, AttrRole, AddAttrRole, MetaRole = 0, 1, 2, 3

    auto_commit = settings.Setting(True)

    selection_indices = settings.Setting(None, schema_only=True)

    legend_anchor = settings.Setting(((1, 0), (1, 0)))

    graph = SettingProvider(OWMDSGraph)

    jitter_sizes = [0, 0.1, 0.5, 1, 2, 3, 4, 5, 7, 10]

    graph_name = "graph.plot_widget.plotItem"

    class Error(OWWidget.Error):
        not_enough_rows = Msg("Input data needs at least 2 rows")
        no_attributes = Msg("Data has no attributes")
        out_of_memory = Msg("Out of memory")
        optimization_error = Msg("Error during optimization\n{}")

    def __init__(self):
        super().__init__()
        #: Effective data used for plot styling/annotations.
        self.data = None  # type: Optional[Orange.data.Table]
        #: Input subset data table
        self.subset_data = None  # type: Optional[Orange.data.Table]
        #: Input data table
        self.signal_data = None

        self._subset_mask = None  # type: Optional[np.ndarray]
        self._invalidated = False
        self.effective_matrix = None
        self._curve = None
        self._primitive_metas = ()
        self._data_metas = None

        self.variable_x = ContinuousVariable("tsne-x")
        self.variable_y = ContinuousVariable("tsne-y")

        self.__update_loop = None
        # timer for scheduling updates
        self.__timer = QTimer(self, singleShot=True, interval=0)
        self.__timer.timeout.connect(self.__next_step)
        self.__state = OWtSNE.Waiting
        self.__in_next_step = False
        self.__draw_similar_pairs = False

        box = gui.vBox(self.controlArea, "t-SNE")
        form = QFormLayout(
            labelAlignment=Qt.AlignLeft,
            formAlignment=Qt.AlignLeft,
            fieldGrowthPolicy=QFormLayout.AllNonFixedFieldsGrow,
            verticalSpacing=10
        )

        form.addRow(
            "Max iterations:",
            gui.spin(box, self, "max_iter", 250, 10 ** 4, step=1))

        form.addRow(
            "Perplexity:",
            gui.spin(box, self, "perplexity", 1, 99, step=1))

        # form.addRow(
        #     "Initialization:",
        #     gui.radioButtons(box, self, "initialization", btnLabels=("PCA", "Random"),
        #                      callback=self.__invalidate_embedding))

        box.layout().addLayout(form)
        # form.addRow(
        #     "Refresh:",
        #     gui.comboBox(box, self, "refresh_rate", items=[t for t, _ in OWtSNE.RefreshRate],
        #                  callback=self.__invalidate_refresh))
        gui.separator(box, 10)
        self.runbutton = gui.button(box, self, "Run", callback=self._toggle_run)

        box = gui.vBox(self.controlArea, "PCA Preprocessing")
        gui.hSlider(box, self, 'pca_components', label="Components: ",
                    minValue=2, maxValue=50, step=1) #, callback=self._initialize)

        box = gui.vBox(self.mainArea, True, margin=0)
        self.graph = OWMDSGraph(self, box, "MDSGraph", view_box=MDSInteractiveViewBox)
        box.layout().addWidget(self.graph.plot_widget)
        self.plot = self.graph.plot_widget

        g = self.graph.gui
        box = g.point_properties_box(self.controlArea)
        self.models = g.points_models

        g.add_widgets(ids=[g.JitterSizeSlider], widget=box)

        box = gui.vBox(self.controlArea, "Plot Properties")
        g.add_widgets([g.ShowLegend,
                       g.ToolTipShowsAll,
                       g.ClassDensity,
                       g.LabelOnlySelected], box)

        self.controlArea.layout().addStretch(100)
        self.icons = gui.attributeIconDict

        palette = self.graph.plot_widget.palette()
        self.graph.set_palette(palette)

        gui.rubber(self.controlArea)

        self.graph.box_zoom_select(self.controlArea)

        gui.auto_commit(box, self, "auto_commit", "Send Selected",
                        checkbox_label="Send selected automatically",
                        box=None)

        self.plot.getPlotItem().hideButtons()
        self.plot.setRenderHint(QPainter.Antialiasing)

        self.graph.jitter_continuous = True
        self._initialize()

    def reset_graph_data(self, *_):
        if self.data is not None:
            self.graph.rescale_data()
            self.update_graph()

    def update_colors(self):
        pass

    def update_density(self):
        self.update_graph(reset_view=False)

    def update_regression_line(self):
        self.update_graph(reset_view=False)

    def init_attr_values(self):
        domain = self.data and len(self.data) and self.data.domain or None
        for model in self.models:
            model.set_domain(domain)
        self.graph.attr_color = self.data.domain.class_var if domain else None
        self.graph.attr_shape = None
        self.graph.attr_size = None
        self.graph.attr_label = None
        self.models[2][:] = self.models[2][0:1] + ["Stress"] + self.models[2][1:]

    def prepare_data(self):
        pass

    def update_graph(self, reset_view=True, **_):
        self.graph.zoomStack = []
        if self.graph.data is None:
            return
        self.graph.update_data(self.variable_x, self.variable_y, True)

    def selection_changed(self):
        self.commit()

    @Inputs.data
    @check_sql_input
    def set_data(self, data):
        """Set the input data set.

        Parameters
        ----------
        data : Optional[Orange.data.Table]
        """
        if data is not None and len(data) < 2:
            self.Error.not_enough_rows()
            data = None
        else:
            self.Error.not_enough_rows.clear()

        self.signal_data = data
        self._invalidated = True

        if data is not None:
            self._primitive_metas = tuple(a for a in data.domain.metas
                                          if a.is_primitive())
            keys = [k for k, a in enumerate(data.domain.metas)
                    if a.is_primitive()]
            self._data_metas = data.metas[:, keys]
        else:
            self._primitive_metas = ()
            self._data_metas = None

    @Inputs.data_subset
    def set_subset_data(self, subset_data):
        """Set a subset of `data` input to highlight in the plot.

        Parameters
        ----------
        subset_data: Optional[Orange.data.Table]
        """
        self.subset_data = subset_data
        # invalidate the pen/brush when the subset is changed
        self._subset_mask = None  # type: Optional[np.ndarray]
        self.controls.graph.alpha_value.setEnabled(subset_data is None)

    def _clear(self):
        self.__set_update_loop(None)
        self.__state = OWtSNE.Waiting

    def _clear_plot(self):
        self.graph.plot_widget.clear()

    def _initialize(self):
        # clear everything
        self.closeContext()
        self._clear()
        self.Error.clear()
        self.data = None
        self.effective_matrix = None
        self.embedding = None
        self.init_attr_values()

        # if no data, reset plot
        if self.signal_data is None:
            return

        self.data = self.signal_data

        if self.data.domain.attributes:
            pca = Orange.projection.PCA(n_components=self.pca_components)
            model = pca(self.data)
            self.effective_matrix = model(self.data)
        else:
            self.Error.no_attributes()
            return

        self.init_attr_values()
        self.openContext(self.data)

    def _toggle_run(self):
        if self.__state == OWtSNE.Running:
            self.stop()
            self._invalidate_output()
        else:
            self.start()

    def start(self):
        if self.__state == OWtSNE.Running:
            return
        elif self.__state == OWtSNE.Finished:
            # Resume/continue from a previous run
            self.__start()
        elif self.__state == OWtSNE.Waiting and \
                self.effective_matrix is not None:
            self.__start()

    def stop(self):
        if self.__state == OWtSNE.Running:
            self.__set_update_loop(None)

    def __start(self):
        embedding = self.embedding

        # number of iterations per single GUI update step
        _, step_size = OWtSNE.RefreshRate[self.refresh_rate]

        ## TODO: refresh rates - fixed to None for now
        step_size = -1

        if step_size == -1:
            step_size = self.max_iter

        def update_loop(X, max_iter, step, embedding):
            """
            return an iterator over successive improved MDS point embeddings.
            """
            # NOTE: this code MUST NOT call into QApplication.processEvents
            done = False
            iterations_done = 0
            # init = "pca" if self.initialization == OWtSNE.PCA else "random"
            init = "pca"

            while not done:
                step_iter = min(max_iter - iterations_done, step)
                tsne = Orange.projection.TSNE(
                    n_components=2, perplexity=self.perplexity,
                    n_iter=step_iter, init=init)

                tsnefit = tsne(X)
                iterations_done += step_iter
                embedding = tsnefit.embedding_

                if iterations_done >= max_iter:
                    done = True

                yield embedding, iterations_done / max_iter

        self.__set_update_loop(update_loop(
            self.effective_matrix, self.max_iter, step_size, embedding))
        self.progressBarInit(processEvents=None)

    def __set_update_loop(self, loop):
        """
        Set the update `loop` coroutine.

        The `loop` is a generator yielding `(embedding, stress, progress)`
        tuples where `embedding` is a `(N, 2) ndarray` of current updated
        MDS points, `stress` is the current stress and `progress` a float
        ratio (0 <= progress <= 1)

        If an existing update coroutine loop is already in palace it is
        interrupted (i.e. closed).

        .. note::
            The `loop` must not explicitly yield control flow to the event
            loop (i.e. call `QApplication.processEvents`)

        """
        if self.__update_loop is not None:
            self.__update_loop.close()
            self.__update_loop = None
            self.progressBarFinished(processEvents=None)

        self.__update_loop = loop

        if loop is not None:
            self.setBlocking(True)
            self.progressBarInit(processEvents=None)
            self.setStatusMessage("Running")
            self.runbutton.setText("Stop")
            self.__state = OWtSNE.Running
            self.__timer.start()
        else:
            self.setBlocking(False)
            self.setStatusMessage("")
            self.runbutton.setText("Start")
            self.__state = OWtSNE.Finished
            self.__timer.stop()

    def __next_step(self):
        if self.__update_loop is None:
            return

        assert not self.__in_next_step
        self.__in_next_step = True

        loop = self.__update_loop
        self.Error.out_of_memory.clear()
        self.Error.optimization_error.clear()
        try:
            embedding, progress = next(self.__update_loop)
            assert self.__update_loop is loop
        except StopIteration:
            self.__set_update_loop(None)
            self.unconditional_commit()
            self._update_plot()
        except MemoryError:
            self.Error.out_of_memory()
            self.__set_update_loop(None)
        except Exception as exc:
            self.Error.optimization_error(str(exc))
            self.__set_update_loop(None)
        else:
            self.progressBarSet(100.0 * progress, processEvents=None)
            self.embedding = embedding
            self._update_plot()
            # schedule next update
            self.__timer.start()

        self.__in_next_step = False

    def __invalidate_embedding(self):
        # reset/invalidate the MDS embedding, to the default initialization
        # (Random or PCA), restarting the optimization if necessary.
        if self.embedding is None:
            return
        state = self.__state
        if self.__update_loop is not None:
            self.__set_update_loop(None)

        if self.initialization == OWtSNE.PCA:
            self.embedding = self.effective_matrix.X[:, :2]
        else:
            self.embedding = np.random.rand(len(self.effective_matrix), 2)

        self._update_plot()

        # restart the optimization if it was interrupted.
        if state == OWtSNE.Running:
            self.__start()

    def __invalidate_refresh(self):
        state = self.__state

        if self.__update_loop is not None:
            self.__set_update_loop(None)

        # restart the optimization if it was interrupted.
        # TODO: decrease the max iteration count by the already
        # completed iterations count.
        if state == OWtSNE.Running:
            self.__start()

    def handleNewSignals(self):
        if self._invalidated:
            self._invalidated = False
            self._initialize()
            self.start()

        if self._subset_mask is None and self.subset_data is not None and \
                self.data is not None:
            self._subset_mask = np.in1d(self.data.ids, self.subset_data.ids)

        self._update_plot(new=True)
        self.unconditional_commit()

    def _invalidate_output(self):
        self.commit()

    def _on_connected_changed(self):
        self._update_plot()

    def _update_plot(self, new=False):
        self._clear_plot()

        if self.embedding is not None:
            self._setup_plot(new=new)
        else:
            self.graph.new_data(None)

    def _setup_plot(self, new=False):
        emb_x, emb_y = self.embedding[:, 0], self.embedding[:, 1]
        coords = np.vstack((emb_x, emb_y)).T
        attributes = (self.data.domain.attributes +
                      (self.variable_x, self.variable_y) +
                      self._primitive_metas)
        domain = Domain(attributes=attributes,
                        class_vars=self.data.domain.class_vars)
        if self._data_metas is not None:
             data_x = (self.data.X, coords, self._data_metas)
        else:
             data_x = (self.data.X, coords)
        data = Table.from_numpy(domain, X=np.hstack(data_x),
                                Y = self.data.Y)
        subset_data = data[self._subset_mask] if self._subset_mask is not None else None
        self.graph.new_data(data, subset_data=subset_data, new=new)
        self.graph.update_data(self.variable_x, self.variable_y, True)

    def commit(self):
        if self.embedding is not None:
            names = get_unique_names([v.name for v in self.data.domain], ["tsne-x", "tsne-y"])
            output = embedding = Orange.data.Table.from_numpy(
                Orange.data.Domain([ContinuousVariable(names[0]), ContinuousVariable(names[1])]),
                self.embedding
            )
        else:
            output = embedding = None

        if self.embedding is not None and self.data is not None:
            domain = self.data.domain
            domain = Orange.data.Domain(domain.attributes,
                                        domain.class_vars,
                                        domain.metas + embedding.domain.attributes)
            output = self.data.transform(domain)
            output.metas[:, -2:] = embedding.X

        selection = self.graph.get_selection()
        if output is not None and len(selection) > 0:
            selected = output[selection]
        else:
            selected = None
        self.Outputs.selected_data.send(selected)
        self.Outputs.annotated_data.send(create_annotated_table(output, selection))

    def onDeleteWidget(self):
        super().onDeleteWidget()
        self._clear_plot()
        self._clear()

    def send_report(self):
        if self.data is None:
            return

        def name(var):
            return var and var.name

        caption = report.render_items_vert((
            ("Color", name(self.graph.attr_color)),
            ("Label", name(self.graph.attr_label)),
            ("Shape", name(self.graph.attr_shape)),
            ("Size", name(self.graph.attr_size)),
            ("Jittering", self.graph.jitter_size != 0 and "{} %".format(self.graph.jitter_size))))
        self.report_plot()
        if caption:
            self.report_caption(caption)


def main(argv=None):
    if argv is None:
        argv = sys.argv
    import gc
    app = QApplication(list(argv))
    argv = app.arguments()
    if len(argv) > 1:
        filename = argv[1]
    else:
        filename = "iris"

    data = Orange.data.Table(filename)
    w = OWtSNE()
    w.set_data(data)
    w.set_subset_data(data[np.random.choice(len(data), 10)])
    w.handleNewSignals()

    w.show()
    w.raise_()
    rval = app.exec_()

    w.set_subset_data(None)
    w.set_data(None)
    w.handleNewSignals()

    w.saveSettings()
    w.onDeleteWidget()
    w.deleteLater()
    del w
    gc.collect()
    app.processEvents()
    return rval

if __name__ == "__main__":
    sys.exit(main())
