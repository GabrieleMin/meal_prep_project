#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <math.h>

/* Fast rounding down to hundredths: math.floor(round(val * 100, 6)) / 100 */
static inline double c_round_down(double value) {
    double v100 = value * 100.0;
    double rounded = round(v100 * 1000000.0) / 1000000.0;
    return floor(rounded) / 100.0;
}

static PyObject* py_round_down_to_hundredth(PyObject* self, PyObject* args) {
    double val;
    if (!PyArg_ParseTuple(args, "d", &val))
        return NULL;
    return PyFloat_FromDouble(c_round_down(val));
}

#define SET_FLOAT(dict, key, val) do { \
    PyObject* _f = PyFloat_FromDouble(val); \
    if (_f) { \
        PyDict_SetItem(dict, key, _f); \
        Py_DECREF(_f); \
    } \
} while(0)

static PyObject* py_sum_macros(PyObject* self, PyObject* args) {
    PyObject* list_obj;
    if (!PyArg_ParseTuple(args, "O!", &PyList_Type, &list_obj))
        return NULL;
    Py_ssize_t n = PyList_GET_SIZE(list_obj);
    double kcal = 0.0, prot = 0.0, carbs = 0.0, fat = 0.0;
    double sug = 0.0, sat_fat = 0.0, fib = 0.0, salt = 0.0;

    static PyObject *k_kcal = NULL, *k_prot = NULL, *k_carbs = NULL, *k_fat = NULL;
    static PyObject *k_sug = NULL, *k_sat_fat = NULL, *k_fib = NULL, *k_salt = NULL;
    if (!k_kcal) {
        k_kcal = PyUnicode_InternFromString("Kcal");
        k_prot = PyUnicode_InternFromString("Protein");
        k_carbs = PyUnicode_InternFromString("Carbs");
        k_fat = PyUnicode_InternFromString("Fat");
        k_sug = PyUnicode_InternFromString("Sugars");
        k_sat_fat = PyUnicode_InternFromString("Saturated_fat");
        k_fib = PyUnicode_InternFromString("Fiber");
        k_salt = PyUnicode_InternFromString("Salt");
    }

    for (Py_ssize_t i = 0; i < n; i++) {
        PyObject* item = PyList_GET_ITEM(list_obj, i);
        if (!PyDict_Check(item)) continue;
        PyObject* v;
        if ((v = PyDict_GetItemWithError(item, k_kcal)) && PyFloat_Check(v)) kcal += PyFloat_AS_DOUBLE(v);
        if ((v = PyDict_GetItemWithError(item, k_prot)) && PyFloat_Check(v)) prot += PyFloat_AS_DOUBLE(v);
        if ((v = PyDict_GetItemWithError(item, k_carbs)) && PyFloat_Check(v)) carbs += PyFloat_AS_DOUBLE(v);
        if ((v = PyDict_GetItemWithError(item, k_fat)) && PyFloat_Check(v)) fat += PyFloat_AS_DOUBLE(v);
        if ((v = PyDict_GetItemWithError(item, k_sug)) && PyFloat_Check(v)) sug += PyFloat_AS_DOUBLE(v);
        if ((v = PyDict_GetItemWithError(item, k_sat_fat)) && PyFloat_Check(v)) sat_fat += PyFloat_AS_DOUBLE(v);
        if ((v = PyDict_GetItemWithError(item, k_fib)) && PyFloat_Check(v)) fib += PyFloat_AS_DOUBLE(v);
        if ((v = PyDict_GetItemWithError(item, k_salt)) && PyFloat_Check(v)) salt += PyFloat_AS_DOUBLE(v);
    }

    PyObject* res = PyDict_New();
    if (!res) return NULL;
    SET_FLOAT(res, k_kcal, kcal);
    SET_FLOAT(res, k_prot, prot);
    SET_FLOAT(res, k_carbs, carbs);
    SET_FLOAT(res, k_fat, fat);
    SET_FLOAT(res, k_sug, sug);
    SET_FLOAT(res, k_sat_fat, sat_fat);
    SET_FLOAT(res, k_fib, fib);
    SET_FLOAT(res, k_salt, salt);
    return res;
}

static PyObject* py_score(PyObject* self, PyObject* args) {
    double coverage, new_packages, share_deviation;
    if (!PyArg_ParseTuple(args, "ddd", &coverage, &new_packages, &share_deviation))
        return NULL;
    double s = 3.0 * coverage - 1.0 * new_packages - 5.0 * share_deviation;
    return PyFloat_FromDouble(s);
}

static PyMethodDef SpeedupMethods[] = {
    {"round_down_to_hundredth", py_round_down_to_hundredth, METH_VARARGS, "Fast hundredth round down in C"},
    {"sum_macros", py_sum_macros, METH_VARARGS, "Fast sum macros in C"},
    {"score", py_score, METH_VARARGS, "Fast score calculation in C"},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef speedupmodule = {
    PyModuleDef_HEAD_INIT,
    "_speedups",
    "C speedups for meal planner",
    -1,
    SpeedupMethods
};

PyMODINIT_FUNC PyInit__speedups(void) {
    return PyModule_Create(&speedupmodule);
}
