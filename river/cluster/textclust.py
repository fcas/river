from __future__ import annotations

import math

import numpy as np
import pandas as pd

from river import base

__all__ = ["TextClust"]


class TextClust(base.Clusterer):
    r"""textClust, a clustering algorithm for text data.

    textClust [^1][^2] is a stream clustering algorithm for textual data that can identify and track topics
    over time in a stream of texts. The algorithm uses a widely popular two-phase clustering
    approach where the stream is first summarised in real-time.

    The result is many small preliminary clusters in the stream called `micro-clusters`.
    Micro-clusters maintain enough information to update and efficiently calculate the
    cosine similarity between them over time, based on the TF-IDF vector of their texts.
    Upon request, the miro-clusters can be reclustered to generate the final result
    using any distance-based clustering algorithm, such as hierarchical clustering.
    To keep the micro-clusters up-to-date, our algorithm applies a fading strategy where
    micro-clusters that are not updated regularly lose relevance and are eventually removed.

    Parameters
    ----------
    radius
        Distance threshold to merge two micro-clusters. Must be within the range `(0, 1]`
    fading_factor
        Fading factor of micro-clusters
    tgap
       Time between outlier removal
    term_fading
        Determines whether individual terms should also be faded
    real_time_fading
        Parameter that specifies whether natural time or the number of observations should be used
        for fading
    micro_distance
         Distance metric used for clustering macro-clusters
    macro_distance
        Distance metric used for clustering macro-clusters
    num_macro
        Number of macro clusters that should be identified during the reclustering phase
    min_weight
        Minimum weight of micro clusters to be used for reclustering
    auto_r
        Parameter that specifies if  `radius` should be automatically updated
    auto_merge
        Determines, if close observations shall be merged together
    sigma
        Parameter that influences the automated trheshold adaption technique

     Attributes
    ----------
    micro_clusters
        Micro-clusters generated by the algorithm. Micro-clusters are of type `textclust.microcluster`

    References
    ----------
    [^1]: Assenmacher, D. und Trautmann, H. (2022). Textual One-Pass Stream Clustering with
    Automated Distance Threshold Adaption. In: Asian Conference on Intelligent Information and
    Database Systems (Accepted)
    [^2]: Carnein, M., Assenmacher, D., Trautmann, H. (2017). Stream Clustering of Chat Messages with
    Applications to Twitch Streams. In: Advances in Conceptual Modeling. ER 2017.

    Examples
    --------

    >>> from river import compose
    >>> from river import feature_extraction
    >>> from river import metrics
    >>> from river import cluster

    >>> corpus = [
    ...    {"text":'This is the first document.',"idd":1, "cluster": 1, "cluster":1},
    ...    {"text":'This document is the second document.',"idd":2,"cluster": 1},
    ...    {"text":'And this is super unrelated.',"idd":3,"cluster": 2},
    ...    {"text":'Is this the first document?',"idd":4,"cluster": 1},
    ...    {"text":'This is super unrelated as well',"idd":5,"cluster": 2},
    ...    {"text":'Test text',"idd":6,"cluster": 5}
    ... ]

    >>> stopwords = [ 'stop', 'the', 'to', 'and', 'a', 'in', 'it', 'is', 'I']

    >>> metric = metrics.AdjustedRand()

    >>> model = compose.Pipeline(
    ...     feature_extraction.BagOfWords(lowercase=True, ngram_range=(1, 2), stop_words=stopwords),
    ...     cluster.TextClust(real_time_fading=False, fading_factor=0.001, tgap=100, auto_r=True,
    ...     radius=0.9)
    ... )

    >>> for x in corpus:
    ...     y_pred = model.predict_one(x["text"])
    ...     y = x["cluster"]
    ...     metric = metric.update(y,y_pred)
    ...     model = model.learn_one(x["text"])

    >>> print(metric)
    AdjustedRand: -0.17647058823529413


    """

    # constructor with default specification
    def __init__(
        self,
        radius=0.3,
        fading_factor=0.0005,
        tgap=100,
        term_fading=True,
        real_time_fading=True,
        micro_distance="tfidf_cosine_distance",
        macro_distance="tfidf_cosine_distance",
        num_macro=3,
        min_weight=0,
        auto_r=False,
        auto_merge=True,
        sigma=1,
    ):
        self.radius = radius
        self.fading_factor = fading_factor
        self.tgap = tgap
        self.term_fading = term_fading
        self.micro_distance = micro_distance
        self.macro_distance = macro_distance
        self.num_macro = num_macro
        self.real_time_fading = real_time_fading
        self.min_weight = min_weight
        self.auto_r = auto_r
        self.auto_merge = auto_merge
        self.sigma = sigma

        # Initialize important values

        self.t = None
        self.last_cleanup = 0
        self.n = 1
        self.omega = 2 ** (-1 * self.fading_factor * self.tgap)
        self.micro_clusters = dict()
        self.microToMacro = None
        self.realtime = None

        self._clusterId = 0
        self._up_to_date = False
        self._dist_mean = 0
        self._num_merged_obs = 0

        # create a new distance instance for micro and macro distances.
        self.micro_distance = self.distances(self.micro_distance)
        self.macro_distance = self.distances(self.macro_distance)

    def learn_one(self, x, t=None, w=None):
        localdict = {}
        for key in x.keys():
            new_key = key
            localdict[new_key] = {}
            localdict[new_key]["tf"] = x[key]
        ngrams = localdict
        ngrams = dict(ngrams)

        # set up to date variable. it is set when everything is faded
        self._up_to_date = False

        # check if realtime fading is on or not. specify current time accordingly
        if self.real_time_fading:
            self.t = t
        else:
            self.t = self.n

        # realtime is only the current time non decoded to store for the plotter
        if self.realtime is not None:
            self.realtime = self.realtime
        clusterId = None
        # if there is something to process
        if len(ngrams) > 0:
            # create artificial micro cluster with one observation
            mc = self.microcluster(ngrams, self.t, 1, self.realtime, self._clusterId)

            # calculate idf
            idf = self._calculateIDF(self.micro_clusters.values())

            clusterId, min_dist = self._get_closest_mc(mc, idf, self.micro_distance)

            # if we found a cluster that is close enough we merge our incoming data into it
            if clusterId is not None:
                self._num_merged_obs += 1
                ## add number of observations
                self.micro_clusters[clusterId].n += 1

                self.micro_clusters[clusterId].merge(
                    mc, self.t, self.omega, self.fading_factor, self.term_fading, self.realtime
                )
                self._dist_mean += min_dist

            # if no close cluster is found we  create a new one
            else:
                self._dist_mean += min_dist
                clusterId = self._clusterId
                self.micro_clusters[clusterId] = mc
                self._clusterId += 1

        # cleanup every tgap
        if self.last_cleanup is None or self.t - self.last_cleanup >= self.tgap:
            self._cleanup()

        ## increment observation counter
        self.n += 1
        return clusterId

    ## predicts the cluster number. The type specifies whether this should happen on micro-cluster
    ## or macro-cluster level
    def predict_one(self, x, w=None, type="micro"):
        localdict = {}
        for key in x.keys():
            new_key = key
            localdict[new_key] = {}
            localdict[new_key]["tf"] = x[key]

        ngrams = localdict
        ngrams = dict(ngrams)

        return self.get_assignment(ngrams, type=type)

    # finds the closest micro cluster
    def _get_closest_mc(self, mc, idf, distance):
        """Return the closest micro cluster."""

        ## initial variable values
        clusterId = None
        min_dist = 1
        smallest_key = None
        sumdist = 0
        squaresum = 0
        counter = 0

        # calculate distances and choose the smallest one
        for key in self.micro_clusters.keys():
            dist = distance.dist(mc, self.micro_clusters[key], idf)

            counter = counter + 1
            sumdist += dist
            squaresum += dist**2

            ## store minimum distance and smallest key
            if dist < min_dist:
                min_dist = dist
                smallest_key = key

        ## if auto threshold is set, we determine the new threshold
        if self.auto_r:
            ## if we at least have two close micro clusters
            if counter > 1:
                ## our threshold
                mu = (sumdist - min_dist) / (counter - 1)
                threshold = mu - self.sigma * math.sqrt(squaresum / (counter - 1) - mu**2)

                if min_dist < threshold:
                    clusterId = smallest_key
        else:
            if min_dist < self.radius:
                clusterId = smallest_key

        return clusterId, min_dist

    # calculate IDF based on micro-cluster weights
    def _calculateIDF(self, micro_clusters):
        result = {}
        for micro in micro_clusters:
            for k in list(micro.tf.keys()):
                if k not in result:
                    result[k] = 1
                else:
                    result[k] += 1
        for k in list(result.keys()):
            result[k] = 1 + math.log(len(micro_clusters) / result[k])
        return result

    # update weights according to the fading factor
    def _updateweights(self):
        for micro in self.micro_clusters.values():
            micro.fade(self.t, self.omega, self.fading_factor, self.term_fading, self.realtime)

        # delete micro clusters with a weight smaller omega
        for key in list(self.micro_clusters.keys()):
            if (
                self.micro_clusters[key].weight <= self.omega
                or len(self.micro_clusters[key].tf) == 0
            ):
                del self.micro_clusters[key]

    # cleanup procedure
    def _cleanup(self):
        # set last cleanup to now
        self.last_cleanup = self.t

        # update current cluster weights
        self._updateweights()

        # set deltaweights
        for micro in self.micro_clusters.values():
            # here we compute delta weights
            micro.deltaweight = micro.weight - micro.oldweight
            micro.oldweight = micro.weight

        # if auto merge is enabled, close micro clusters are merged together
        if self.auto_merge:
            self._mergemicroclusters()

        ## reset merged observation
        self._dist_mean = 0
        self._num_merged_obs = 0

    # merge
    def _mergemicroclusters(self):
        micro_keys = [*self.micro_clusters]

        idf = self._calculateIDF(self.micro_clusters.values())
        i = 0
        if self.auto_r:
            threshold = self._dist_mean / (self._num_merged_obs + 1)
        else:
            threshold = self.radius

        while i < len(self.micro_clusters):
            j = i + 1
            while j < len(self.micro_clusters):
                m_dist = self.micro_distance.dist(
                    self.micro_clusters[micro_keys[i]], self.micro_clusters[micro_keys[j]], idf
                )

                ## lets merge them
                if m_dist < threshold:
                    self.micro_clusters[micro_keys[i]].merge(
                        self.micro_clusters[micro_keys[j]],
                        self.t,
                        self.omega,
                        self.fading_factor,
                        self.term_fading,
                        self.realtime,
                    )

                    ## if two microclusters are merged we keep track of the ids
                    self.micro_clusters[micro_keys[i]].merged_ids.append(
                        self.micro_clusters[micro_keys[j]].id
                    )
                    del self.micro_clusters[micro_keys[j]]
                    del micro_keys[j]
                else:
                    j = j + 1
            i = i + 1

    # calculate a distance matrix from all provided micro clusters
    def _get_distance_matrix(self, clusters):
        # if we need IDF for our distance calculation, we calculate it from the micro clusters
        idf = self._calculateIDF(clusters.values())

        # get number of clusters
        numClusters = len(clusters)
        ids = list(clusters.keys())

        # initialize all distances to 0
        distances = pd.DataFrame(np.zeros((numClusters, numClusters)), columns=ids, index=ids)

        for idx, row in enumerate(ids):
            for col in ids[idx + 1 :]:
                # use the macro-distance metric to calculate the distances to different micro-clusters
                dist = self.macro_distance.dist(clusters[row], clusters[col], idf)
                distances.loc[row, col] = dist
                distances.loc[col, row] = dist

        return distances

    # This is a greedy implementation of single linkage agglomerative clustering. In the future we
    # will make this function more flexible
    def _agglomerative_clustering(self, micros, k):
        clusters = []

        ## calculate distance matrix
        distm = self._get_distance_matrix(micros)

        indices = distm.index

        ## init empty clusters
        for i in range(0, len(micros)):
            clusters.append([indices[i]])

        ## repeat until the number of clusters k are formed
        while len(clusters) != k:
            min_dist = math.inf
            min_pair = ()

            ## iterate over all current sets
            for i in range(0, len(clusters) - 1):
                for j in range(i + 1, len(clusters)):
                    ## iterate over all clusters in sets
                    for c_i in clusters[i]:
                        for c_j in clusters[j]:
                            if distm[c_i][c_j] < min_dist:
                                min_dist = distm[c_i][c_j]
                                min_pair = (i, j)

            ## now merge
            clusters[min_pair[0]] = clusters[min_pair[0]] + clusters[min_pair[1]]
            del clusters[min_pair[1]]

        return clusters

    def updateMacroClusters(self):
        # check if something changed since last reclustering
        if not self._up_to_date:
            # first update the weights
            self._updateweights()

            # filter for weight threshold and discard outlier or emerging micro clusters
            micros = {
                key: value
                for key, value in self.micro_clusters.items()
                if value.weight > self.min_weight
            }

            ## if the number of micro clusters is smaller than num_macro, we have to adjust
            numClusters = min([self.num_macro, len(micros)])

            if (len(micros)) > 1:
                # right now we use hierarchical clustering single linkage.
                assigned_clusters = self._agglomerative_clustering(micros, numClusters)

                # build micro to macro cluster assignment based on key and clustering result
                self.microToMacro = {}
                for i in range(0, len(assigned_clusters)):
                    for x in assigned_clusters[i]:
                        self.microToMacro[x] = i

                self._up_to_date = True

    # here we get macro cluster representatives by merging according to microToMacro assignments
    def get_macroclusters(self):
        self.updateMacroClusters()
        numClusters = min([self.num_macro, len(self.micro_clusters)])

        # create empty clusters
        macros = {x: self.microcluster({}, self.t, 0, self.realtime, x) for x in range(numClusters)}

        # merge micro clusters to macro clusters
        for key, value in self.microToMacro.items():
            macros[value].merge(
                self.micro_clusters[key],
                self.t,
                self.omega,
                self.fading_factor,
                self.term_fading,
                self.realtime,
            )
            macros[value].merged_ids.append(self.micro_clusters[key].id)

        return macros

    # show top micro/macro clusters (according to weight)
    def showclusters(self, topn, num, type="micro"):
        # first clusters are sorted according to their respective weights
        if type == "micro":
            sortedmicro = sorted(self.micro_clusters.values(), key=lambda x: x.weight, reverse=True)
        else:
            sortedmicro = sorted(
                self.get_macroclusters().values(), key=lambda x: x.weight, reverse=True
            )

        print("-------------------------------------------")
        print("Summary of " + type + " clusters:")

        for micro in sortedmicro[0:topn]:
            print("----")
            print(type + " cluster id " + str(micro.id))
            print(type + " cluster weight " + str(micro.weight))
            if type != "micro":
                print("merged micro clusters: " + str(micro.merged_ids))

            # get indices of top terms
            indices = sorted(
                range(len([i["tf"] for i in micro.tf.values()])),
                key=[i["tf"] for i in micro.tf.values()].__getitem__,
                reverse=True,
            )

            # get representative and weight for micro cluster (room for improvement here?)
            representatives = [
                (list(micro.tf.keys())[i], micro.tf[list(micro.tf.keys())[i]]["tf"])
                for i in indices[0 : min(len(micro.tf.keys()), num)]
            ]
            for rep in representatives:
                print(
                    "weight: " + str(round(rep[1], 2)) + "\t token: " + str(rep[0]).expandtabs(10)
                )
        print("-------------------------------------------")

    # for a new observation(s) get the assignment to micro or macro clusters
    def get_assignment(self, x, type):
        self._updateweights()

        # assignment is an empty list
        assignment = None
        idf = None

        idf = self._calculateIDF(self.micro_clusters.values())

        # proceed, if the processed text is not empty
        if len(x) > 0:
            # create temporary micro cluster
            mc = self.microcluster(x, 1, 1, self.realtime, None)

            # initialize distances to infinity
            dist = float("inf")
            closest = None

            # identify the closest micro cluster using the predefined distance measure
            for key in self.micro_clusters.keys():
                if self.micro_clusters[key].weight > self.min_weight:
                    cur_dist = self.micro_distance.dist(mc, self.micro_clusters[key], idf)
                    if cur_dist < dist:
                        dist = cur_dist
                        closest = key

            # add assignment
            assignment = closest

            if type == "micro":
                return assignment

            ## if type is macro then get macro cluster assignment
            else:
                self.updateMacroClusters()
                return self.microToMacro[assignment] if assignment else None

    ## tf container
    class tfcontainer:
        def __init__(self, tfvalue, ids):
            self.tfvalue = tfvalue
            self.ids = ids

    ## micro cluster class
    class microcluster:
        ## Initializer / Instance Attributes
        def __init__(self, tf, time, weight, realtime, clusterid):
            self.id = clusterid
            self.weight = weight
            self.time = time
            self.tf = tf
            self.oldweight = 0
            self.deltaweight = 0
            self.realtime = realtime
            self.n = 1
            self.merged_ids = []

        ## fading micro cluster weights and also term weights, if activated
        def fade(self, tnow, omega, fading_factor, term_fading, realtime):
            self.weight = self.weight * pow(2, -fading_factor * (tnow - self.time))
            if term_fading:
                for k in list(self.tf.keys()):
                    self.tf[k]["tf"] = self.tf[k]["tf"] * pow(
                        2, -fading_factor * (tnow - self.time)
                    )
                    if self.tf[k]["tf"] <= omega:
                        del self.tf[k]
            self.time = tnow
            self.realtime = realtime

        ## merging two microclusters into one
        def merge(self, microcluster, t, omega, fading_factor, term_fading, realtime):
            self.realtime = realtime

            self.weight = self.weight + microcluster.weight

            self.fade(t, omega, fading_factor, term_fading, realtime)
            microcluster.fade(t, omega, fading_factor, term_fading, realtime)

            self.time = t
            # here we merge an existing mc wth the current mc. The tf values as well as the ids have to be transferred
            for k in list(microcluster.tf.keys()):
                if k in self.tf:
                    self.tf[k]["tf"] += microcluster.tf[k]["tf"]
                else:
                    self.tf[k] = {}
                    self.tf[k]["tf"] = microcluster.tf[k]["tf"]

    ## distance class to implement different micro/macro distance metrics
    class distances:
        ## constructor
        def __init__(self, type):
            self.type = type

        ## generic method that is called for each distance
        def dist(self, m1, m2, idf):
            return getattr(self, self.type, lambda: "Invalid distance measure")(m1, m2, idf)

        ##calculate cosine similarity directly and fast
        def tfidf_cosine_distance(self, mc, microcluster, idf):
            sum = 0
            tfidflen = 0
            microtfidflen = 0
            for k in list(mc.tf.keys()):
                if k in idf:
                    if k in microcluster.tf:
                        sum += (mc.tf[k]["tf"] * idf[k]) * (microcluster.tf[k]["tf"] * idf[k])
                    tfidflen += mc.tf[k]["tf"] * idf[k] * mc.tf[k]["tf"] * idf[k]
            tfidflen = math.sqrt(tfidflen)
            for k in list(microcluster.tf.keys()):
                microtfidflen += (
                    microcluster.tf[k]["tf"] * idf[k] * microcluster.tf[k]["tf"] * idf[k]
                )
            microtfidflen = math.sqrt(microtfidflen)
            if tfidflen == 0 or microtfidflen == 0:
                return 1
            else:
                return round((1 - sum / (tfidflen * microtfidflen)), 10)
